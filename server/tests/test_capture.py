"""Generated media validates file handling; it is not a WeChat/AirPlay device test."""

import json
import shutil
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from hidagent.capture import CaptureLab, create_app, inspect_recording


@pytest.fixture
def sample(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required for generated-media tests")

    def make(name="sample.mp4", audio="tone"):
        args = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                "color=c=blue:s=160x288:r=15:d=1.2"]
        if audio:
            source = "sine=frequency=700:sample_rate=44100:duration=1.2" if audio == "tone" else \
                "anullsrc=channel_layout=mono:sample_rate=44100:d=1.2"
            args += ["-f", "lavfi", "-i", source]
        args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(tmp_path / name)]
        subprocess.run(args, check=True, capture_output=True, timeout=30)
        return tmp_path / name

    return make


def test_av_capture_exports_playable_video_and_real_audio(sample):
    source = sample()
    result = inspect_recording(source, 1)
    assert result["has_audio"] and result["has_video"]
    assert result["audio_levels"]["signal_detected"]
    assert 1.1 < result["audio_levels"]["duration_seconds"] < 1.4
    assert (source.parent / result["audio"]).stat().st_size > 1000
    playback = source.parent / result["playback"]
    # Decode the complete playback artifact, not just its container header.
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(playback), "-f", "null", "-"],
                   capture_output=True, check=True, timeout=15)
    assert result["warnings"] == []


def test_video_without_audio_is_not_reported_as_successful_audio_capture(sample):
    result = inspect_recording(sample(audio=None), 1)
    assert result["has_video"] and not result["has_audio"]
    assert "audio" not in result
    assert result["warnings"]
    assert result["playback"]


def test_silent_audio_track_is_distinguished_from_sound(sample):
    result = inspect_recording(sample(audio="silence"), 1)
    assert result["has_audio"]
    assert not result["audio_levels"]["signal_detected"]
    assert result["audio_levels"]["rms_dbfs"] is None


def test_unfinalized_or_invalid_mp4_is_rejected(tmp_path):
    if not shutil.which("ffprobe"):
        pytest.skip("ffprobe required")
    source = tmp_path / "broken.mp4"
    source.write_bytes(b"unfinalized mp4")
    with pytest.raises(RuntimeError):
        inspect_recording(source, 1)


def wait_for(predicate):
    end = time.monotonic() + 8
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.03)
    raise AssertionError("Timed out waiting for fake receiver")


@pytest.fixture
def fake_receiver(tmp_path, monkeypatch):
    script = tmp_path / "fake-uxplay"
    # A real child process verifies graceful stop/reaping; no phone or socket is involved.
    script.write_text(f"#!{sys.executable}\n"
                      "import signal, time\n"
                      "signal.signal(signal.SIGINT, lambda *_: exit(0))\n"
                      "print('READY', flush=True)\n"
                      "while True: time.sleep(.1)\n")
    script.chmod(0o755)
    monkeypatch.setattr("hidagent.screen.uxplay", lambda: str(script))
    return script


def test_capture_lifecycle_rejects_overlap_and_ignores_old_watchers(tmp_path, fake_receiver, sample):
    lab = CaptureLab(tmp_path / "captures")
    first = lab.start()["current"]
    old_proc = lab._proc
    wait_for(lambda: "READY" in (lab.root / first["id"] / "uxplay.log").read_text())
    try:
        with pytest.raises(RuntimeError):
            lab.start()
        shutil.copy(sample(), lab.root / first["id"] / "capture.1.H264.AAC.mp4")
        result = lab.stop()["current"]
        assert result["state"] == "ready"
        assert result["exit_code"] == 0 and old_proc.poll() == 0
        assert len(result["files"]) == 1
        second = lab.start()["current"]
        assert second["id"] != first["id"]
        wait_for(lambda: "READY" in (lab.root / second["id"] / "uxplay.log").read_text())
        lab.stop("receiver_exited", expected_process=old_proc)
        assert lab.status()["current"]["state"] == "listening"
        assert lab.stop()["current"]["state"] == "failed"  # no phone data, not a successful capture
    finally:
        lab.shutdown()


def test_capture_timeout_finalizes_and_reaps_receiver(tmp_path, fake_receiver, sample):
    sample()  # dependency check; this test intentionally receives no media
    lab = CaptureLab(tmp_path / "captures", max_seconds=1)
    lab.start()
    wait_for(lambda: lab.status()["current"]["state"] == "failed")
    assert lab.status()["current"]["stop_reason"] == "time_limit"
    assert lab._proc.poll() is not None
    lab.shutdown()


def test_local_api_blocks_cross_origin_actions_and_unlisted_files(tmp_path):
    lab = CaptureLab(tmp_path)
    recording_id = "a" * 32
    directory = tmp_path / recording_id
    directory.mkdir()
    (directory / "result.json").write_text(json.dumps({"id": recording_id, "state": "ready", "files": []}))
    (directory / "private.txt").write_text("not an artifact")
    with TestClient(create_app(lab)) as client:
        assert client.post("/api/start").status_code == 403
        assert client.get("/api/status", headers={"Host": "untrusted.example"}).status_code == 400
        assert client.get(f"/files/{recording_id}/result.json").status_code == 200
        assert client.get(f"/files/{recording_id}/private.txt").status_code == 404
        assert client.get("/files/not-an-id/result.json").status_code == 404
    with pytest.raises(FileNotFoundError):
        lab.file(recording_id, "../private.txt")


def test_source_files_cannot_escape_capture_directory_via_symlink(tmp_path):
    lab = CaptureLab(tmp_path / "captures")
    recording_id = "b" * 32
    directory = lab.root / recording_id
    directory.mkdir(parents=True)
    outside = tmp_path / "other.mp4"
    outside.write_bytes(b"not this recording")
    (directory / "capture.1.mp4").symlink_to(outside)
    (directory / "result.json").write_text(json.dumps({
        "state": "ready", "files": [{"source": "capture.1.mp4"}],
    }))
    with pytest.raises(FileNotFoundError):
        lab.file(recording_id, "capture.1.mp4")
