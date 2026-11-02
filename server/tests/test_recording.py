import array
import asyncio
import io
import math
import shutil
import time
import wave

import av
import pytest
from PIL import Image

from hidagent.agent import parse_actions
from hidagent.recording import StreamRecorder


def jpeg():
    buffer = io.BytesIO()
    Image.new("RGB", (160, 288), "blue").save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def recorder(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required")
    instance = StreamRecorder(tmp_path, max_seconds=3)
    instance.video(jpeg(), time.time_ns())
    yield instance
    instance.stop()
    instance.wait()


def test_same_feed_records_audio_without_restarting_receiver(recorder):
    recorder.start("manual playback")
    with pytest.raises(RuntimeError):
        recorder.start()
    # 300 ms before playback; verify the gap survives, instead of shifting speech to t=0.
    time.sleep(.3)
    samples = array.array("h", [int(6000 * math.sin(2 * math.pi * 600 * n / 16000)) for n in range(1600)])
    for _ in range(7):
        time.sleep(.1)
        # Media timestamp describes the START of this chunk, independently of delivery time.
        recorder.audio(samples.tobytes(), time.time_ns() - 100_000_000, 16000, 1)
    recorder.stop()
    result = recorder.wait()["current"]
    assert result["state"] == "ready", result
    assert result["files"][0]["has_audio"]
    assert result["files"][0]["audio_levels"]["signal_detected"]
    assert .68 < result["audio_received_seconds"] < .72
    with wave.open(str(recorder.root / result["id"] / "received.wav")) as wav:
        first = array.array("h", wav.readframes(16000))
    onset = next(i for i, value in enumerate(first) if value) / 16000
    assert .25 < onset < .5
    with av.open(str(recorder.root / result["id"] / "video.mp4")) as movie:
        stamps = [float(f.pts * f.time_base) for f in movie.decode(video=0)]
    assert stamps[0] == 0 and stamps[-1] > .9  # static picture spans the audio interval
    assert recorder.file(result["id"], result["files"][0]["playback"]).is_file()


def test_no_audio_is_reported_and_second_capture_is_independent(recorder):
    first = recorder.start()["current"]["id"]
    time.sleep(.2)
    recorder.stop()
    result = recorder.wait()["current"]
    assert result["state"] == "ready"
    assert not result["files"][0]["has_audio"]
    assert result["audio_received_seconds"] == 0
    second = recorder.start()["current"]["id"]
    assert first != second
    time.sleep(.2)
    recorder.stop("agent_cancelled")
    assert recorder.wait()["current"]["state"] == "interrupted"


def test_disconnection_saves_partial_capture(recorder):
    recorder.live = lambda: False
    recorder.start()
    result = recorder.wait(timeout=15)["current"]
    assert result["state"] == "interrupted"
    assert result["stop_reason"] == "connection_lost"
    assert result["files"][0]["has_video"]


def test_recording_action_duration_is_bounded():
    assert parse_actions("DO: record_start\nDO: record_wait 80\nDO: record_stop")[1]["seconds"] == 80
    for n in (-1, 0, 181):
        with pytest.raises(ValueError):
            parse_actions(f"DO: record_wait {n}")


def test_agent_recording_wait_is_cancellable(recorder):
    from types import SimpleNamespace

    from hidagent.agent import Agent

    async def run():
        recorder.start()
        agent = object.__new__(Agent)
        agent.screen = SimpleNamespace(recorder=recorder, live=True)
        task = asyncio.create_task(agent._do({"action": "record_wait", "seconds": 80}))
        await asyncio.sleep(.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


def test_main_api_manual_capture_and_range_download(recorder, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    monkeypatch.setenv("LINK", "sim")
    monkeypatch.setenv("HIDAGENT_DATA", str(tmp_path / "data"))
    from hidagent import app as main

    monkeypatch.setattr(main, "SCREEN", "uxplay")
    monkeypatch.setattr(main, "screen", SimpleNamespace(live=True, recorder=recorder))
    client = TestClient(main.app)
    try:
        response = client.post("/api/recording/start", json={"text": "manual test"})
        assert response.status_code == 200
        assert client.post("/api/recording/start", json={"text": "again"}).status_code == 409
        time.sleep(.2)
        assert client.post("/api/recording/stop").status_code == 200
        result = recorder.wait()["current"]
        assert result["state"] == "ready"
        path = f'/api/recordings/{result["id"]}/{result["files"][0]["playback"]}'
        download = client.get(path, headers={"Range": "bytes=0-15"})
        assert download.status_code == 206 and len(download.content) == 16
        assert client.get(f'/api/recordings/{result["id"]}/received.wav').status_code == 404
        assert client.get("/api/recordings").json()[0]["task"] == "manual test"
    finally:
        client.close()


@pytest.mark.parametrize("rate", [44100, 48000])
def test_batched_native_stereo_is_bit_exact_and_video_uses_same_pts(tmp_path, rate):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required")
    clock = [time.time_ns()]
    recorder = StreamRecorder(tmp_path, max_seconds=20, clock_ns=lambda: clock[0])
    recorder.video(jpeg(), clock[0])
    recorder.start()
    origin = clock[0]
    # 12 seconds arrive immediately in three bursts. The former arrival-time
    # limiter discarded most of this PCM; every native stereo sample must survive.
    block = array.array("h", [v for n in range(rate // 10) for v in (n % 6000 - 3000, 3000 - n % 6000)])
    payload = block.tobytes()
    try:
        for i in range(120):
            recorder.audio(payload, origin + 300_000_000 + i * 100_000_000, rate, 2)
        recorder.video(jpeg(), origin + 500_000_000)
        recorder.video(jpeg(), origin + 6_500_000_000)
        clock[0] = origin + 13_000_000_000
        recorder.stop()
        result = recorder.wait()["current"]
        assert result["state"] == "ready", result
        assert result["timing"] == "source_pts"
        assert result["audio_received_seconds"] == 12
        assert result["audio_gap_frames"] == round(.3 * rate)
        assert result["audio_format"] == {"sample_rate": rate, "channels": 2, "sample_format": "s16le"}
        # Verify the user-downloadable WAV, after lossless MP4 mux/export, not just an internal buffer.
        wav_path = recorder.file(result["id"], result["files"][0]["audio"])
        with wave.open(str(wav_path)) as wav:
            assert wav.getframerate() == rate and wav.getnchannels() == 2
            assert wav.readframes(round(.3 * rate)) == b"\0" * round(.3 * rate) * 4
            assert wav.readframes(12 * rate) == payload * 120
            assert wav.getnframes() == 13 * rate
        with av.open(str(recorder.file(result["id"], "capture.mp4"))) as movie:
            assert movie.streams.audio[0].codec_context.name == "alac"
            stamps = [float(f.pts * f.time_base) for f in movie.decode(video=0)]
        assert any(abs(t - .5) < .001 for t in stamps)
        assert any(abs(t - 6.5) < .001 for t in stamps)
    finally:
        recorder.stop()
        recorder.wait()


def test_late_delivery_preserves_media_gap_and_second_capture_resets_clock(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required")
    clock = [time.time_ns()]
    recorder = StreamRecorder(tmp_path, clock_ns=lambda: clock[0])
    recorder.video(jpeg(), clock[0])
    chunk = b"\x01\x20" * 4800
    try:
        for _ in range(2):
            origin = clock[0]
            recorder.start()
            recorder.audio(chunk, origin + 200_000_000, 48000, 1)
            clock[0] = origin + 1_000_000_000
            recorder.stop()
            time.sleep(.03)
            # Received after stop, but presentation belongs inside the requested interval.
            recorder.audio(chunk, origin + 600_000_000, 48000, 1)
            result = recorder.wait()["current"]
            assert result["state"] == "ready", result
            with wave.open(str(recorder.file(result["id"], result["files"][0]["audio"]))) as wav:
                data = wav.readframes(wav.getnframes())
                assert data[9600 * 2:14400 * 2] == chunk
                assert data[14400 * 2:28800 * 2] == b"\0" * (28800 - 14400) * 2
                assert data[28800 * 2:33600 * 2] == chunk
            assert result["audio_received_seconds"] == .2
            clock[0] += 10_000_000_000
    finally:
        recorder.stop()
        recorder.wait()


def test_missing_source_timestamp_stops_instead_of_guessing(recorder):
    recorder.start()
    recorder.audio(b"\x01\x20" * 1600, None, 16000, 1)
    result = recorder.wait()["current"]
    assert result["state"] == "interrupted"
    assert result["stop_reason"] == "stream_error"
    assert any("时间戳" in e for e in result["errors"])


def test_prefetched_samples_are_cut_only_at_explicit_stop_boundary(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required")
    clock = [time.time_ns()]
    recorder = StreamRecorder(tmp_path, clock_ns=lambda: clock[0])
    recorder.video(jpeg(), clock[0] + 500_000_000)
    recorder.start()
    payload = b"\x01\x20" * 48000
    recorder.audio(payload, clock[0], 48000, 1)
    # Give the worker time to process buffered future PCM BEFORE stop is requested.
    deadline = time.monotonic() + 3
    while recorder.status()["current"]["audio_received_seconds"] != 1:
        assert time.monotonic() < deadline
        time.sleep(.01)
    clock[0] += 750_000_000
    recorder.stop()
    result = recorder.wait()["current"]
    assert result["state"] == "ready", result
    assert result["audio_written_frames"] == 36000
    assert result["boundary_trimmed_frames"] == 12000
    with wave.open(str(recorder.file(result["id"], result["files"][0]["audio"]))) as wav:
        assert wav.readframes(wav.getnframes()) == payload[:36000 * 2]
    with av.open(str(recorder.file(result["id"], "capture.mp4"))) as movie:
        frames = list(movie.decode(video=0))
        # Future seed starts at .5 s; the recording starts with black, not a future picture.
        assert max(frames[0].to_image().getpixel((50, 50))) < 5
        assert any(abs(float(f.pts * f.time_base) - .5) < .001 for f in frames)
