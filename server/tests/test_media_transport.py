import io
import os
import shutil
import subprocess
import threading
import time

import pytest

from hidagent.media_transport import HEADER, capture_plugin_env, read_packets


def packet(data=b"\0\1" * 4, sequence=0, flags=1, pts=1234):
    return HEADER.pack(b"HCAP", 1, 1, flags, pts, 250_000, len(data), 16000, 1, sequence) + data


def test_pipe_fragmentation_keeps_complete_frames_and_original_timestamps():
    class Fragmented(io.BytesIO):
        def read(self, size):
            return super().read(min(size, 3))

    result = list(read_packets(Fragmented(packet() + packet(sequence=1, pts=1484))))
    assert len(result) == 2
    assert [p.pts_ns for p in result] == [1234, 1484]
    assert result[0].data == b"\0\1" * 4
    assert result[0].rate == 16000 and result[0].channels == 1


def test_missing_timestamp_is_explicit_and_bad_transport_fails():
    assert list(read_packets(io.BytesIO(packet(flags=0))))[0].pts_ns is None
    for malformed in (packet()[:-1], packet()[:20], b"nope" + packet()[4:], packet() + packet(sequence=2)):
        with pytest.raises(ValueError):
            list(read_packets(io.BytesIO(malformed)))


@pytest.fixture
def plugin_env():
    if not shutil.which("gst-launch-1.0") or not shutil.which("pkg-config") or not shutil.which("cc"):
        pytest.skip("GStreamer development installation and compiler required")
    if subprocess.run(["pkg-config", "--exists", "gstreamer-base-1.0"]).returncode:
        pytest.skip("GStreamer headers required")
    return capture_plugin_env()


def test_native_sink_preserves_burst_pcm_and_pts_without_clock_pacing(plugin_env, tmp_path):
    read_fd, write_fd = os.pipe()
    received = []
    errors = []

    def reader():
        try:
            with os.fdopen(read_fd, "rb", buffering=0) as pipe:
                received.extend(read_packets(pipe))
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=reader)
    thread.start()
    started = time.time_ns()
    with (tmp_path / "gst.log").open("wb") as log:
        proc = subprocess.Popen([
            "gst-launch-1.0", "-q", "audiotestsrc", "num-buffers=100", "samplesperbuffer=4410", "!",
            "audio/x-raw,format=S16LE,rate=44100,channels=2", "!", "handysink", f"fd={write_fd}", "sync=true",
        ], env=plugin_env, pass_fds=(write_fd,), stdout=log, stderr=log)
        os.close(write_fd)
        try:
            proc.wait(timeout=8)  # 10 seconds of media must be delivered as a burst, not paced.
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    thread.join(timeout=2)
    assert not thread.is_alive() and not errors
    assert proc.returncode == 0, (tmp_path / "gst.log").read_text()
    assert len(received) == 100
    assert sum(len(p.data) for p in received) == 44100 * 2 * 2 * 10
    assert all(p.rate == 44100 and p.channels == 2 and p.pts_ns is not None for p in received)
    assert started - 100_000_000 < received[0].pts_ns < time.time_ns() + 100_000_000
    for first, second in zip(received, received[1:], strict=False):
        assert second.pts_ns - first.pts_ns == 100_000_000
