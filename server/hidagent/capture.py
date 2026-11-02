"""Standalone AirPlay capture validation. No phone control, AI, or IM integration."""

import array
import asyncio
import json
import logging
import math
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import wave
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1] / "out" / "media-captures"
RECEIVER = "iPhone Media Test"
MAX_SECONDS = 180
ACTIVE = {"listening", "finalizing"}


def run_media(args: list[str], timeout: int = 60) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:] or f"{args[0]} exited {result.returncode}")
    return result.stdout


def audio_levels(path: Path) -> dict:
    """Signal energy only: this does not establish speech content or completeness."""
    total = count = peak = 0
    with wave.open(str(path), "rb") as audio:
        if audio.getsampwidth() != 2:
            raise ValueError("Expected 16-bit PCM")
        rate, channels = audio.getframerate(), audio.getnchannels()
        while chunk := audio.readframes(16384):
            samples = array.array("h", chunk)
            if sys.byteorder != "little":
                samples.byteswap()
            count += len(samples)
            total += sum(n * n for n in samples)
            peak = max(peak, max((abs(n) for n in samples), default=0))
    rms = math.sqrt(total / count) / 32768 if count else 0
    return {"duration_seconds": round(count / rate / channels, 3), "sample_rate": rate, "channels": channels,
            "rms_dbfs": round(20 * math.log10(rms), 1) if rms else None,
            "peak_dbfs": round(20 * math.log10(peak / 32768), 1) if peak else None,
            "signal_detected": rms > 0.001}


def inspect_recording(source: Path, index: int) -> dict:
    """Keep the received MP4; produce browser playback and a separately checkable WAV."""
    probe = json.loads(run_media([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height,sample_rate,channels,start_time,duration",
        "-of", "json", str(source),
    ], timeout=15))
    streams = probe.get("streams", [])
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    has_video = any(s.get("codec_type") == "video" for s in streams)
    if not (has_audio or has_video):
        raise ValueError("No audio or video stream in the received file")
    item = {"source": source.name, "bytes": source.stat().st_size, "streams": streams,
            "duration_seconds": float(probe.get("format", {}).get("duration", 0)),
            "has_audio": has_audio, "has_video": has_video, "warnings": []}
    if has_audio:
        wav = source.parent / f"audio-{index}.wav"
        try:
            run_media(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source),
                       "-map", "0:a:0", "-vn", "-c:a", "pcm_s16le", str(wav)])
            item["audio_levels"] = audio_levels(wav)
            item["audio"] = wav.name
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            item["warnings"].append(f"Audio export failed: {exc}")
    else:
        item["warnings"].append("No audio track received; this does not mean the message itself was silent.")
    # AirPlay AAC-ELD is not reliably playable in browsers. Transcode audio to AAC-LC;
    # preserve video packets and their timestamps rather than assembling JPEG screenshots.
    playback = source.parent / f"playback-{index}.mp4"
    try:
        run_media(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source),
                   "-map", "0:v:0?", "-map", "0:a:0?", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k",
                   "-movflags", "+faststart", str(playback)])
        item["playback"] = playback.name
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        item["warnings"].append(f"Browser playback export failed: {exc}")
    return item


class CaptureLab:
    def __init__(self, root: Path = ROOT, max_seconds: int = MAX_SECONDS):
        self.root, self.max_seconds = root, max_seconds
        self._lock = threading.RLock()
        self._finished = threading.Event()
        self._finished.set()
        self._current: dict | None = None
        self._proc: subprocess.Popen | None = None
        self._logfile = None

    def _save(self) -> None:
        path = self.root / self._current["id"] / "result.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._current, ensure_ascii=False, indent=2))
        temporary.replace(path)

    def status(self) -> dict:
        with self._lock:
            current = dict(self._current) if self._current else None
            if current and current["state"] == "listening":
                current["elapsed_seconds"] = round(time.time() - current["started_at"], 1)
                current["received_bytes"] = sum(p.stat().st_size for p in
                                                (self.root / current["id"]).glob("capture.*.mp4"))
            return {"receiver": RECEIVER, "max_seconds": self.max_seconds, "current": current}

    def start(self) -> dict:
        from .screen import uxplay

        with self._lock:
            if not self._finished.is_set() or (self._current and self._current["state"] in ACTIVE):
                raise RuntimeError("已有采集任务，请先停止并等待文件保存。")
            binary = uxplay()
            if not shutil.which(binary) or any(not shutil.which(name) for name in ("ffmpeg", "ffprobe")):
                raise RuntimeError("需要安装 UxPlay、ffmpeg 和 ffprobe。")
            recording_id = uuid.uuid4().hex
            directory = self.root / recording_id
            directory.mkdir(parents=True, mode=0o700)
            self._current = {"id": recording_id, "state": "listening", "started_at": time.time(),
                             "files": [], "errors": [], "receiver": RECEIVER}
            self._finished.clear()
            # An empty rc avoids inheriting a user's mute, HLS or output options. A distinct
            # AirPlay device ID (-m) and ports allow the existing relay to remain running.
            rc = directory / "uxplayrc"
            rc.touch()
            args = [binary, "-rc", str(rc), "-n", RECEIVER, "-nh", "-m", "-p", "7400",
                    "-reset", "0", "-vsync", "no", "-vs", "fakesink", "-as", "fakesink",
                    "-mp4", str(directory / "capture")]
            self._logfile = (directory / "uxplay.log").open("wb")
            try:
                self._proc = subprocess.Popen(args, stdout=self._logfile, stderr=subprocess.STDOUT,
                                              stdin=subprocess.DEVNULL, start_new_session=True)
            except OSError as exc:
                self._logfile.close()
                self._current.update(state="failed", errors=[str(exc)])
                self._save()
                self._finished.set()
                raise RuntimeError(str(exc)) from exc
            self._save()
            threading.Thread(target=self._watch, args=(self._proc,), daemon=True).start()
            return self.status()

    def _watch(self, proc: subprocess.Popen) -> None:
        try:
            proc.wait(timeout=self.max_seconds)
            reason = "receiver_exited"
        except subprocess.TimeoutExpired:
            reason = "time_limit"
        self.stop(reason, expected_process=proc)

    def stop(self, reason: str = "manual", expected_process: subprocess.Popen | None = None) -> dict:
        with self._lock:
            if expected_process is not None and expected_process is not self._proc:
                return self.status()
            if not self._current or self._current["state"] != "listening":
                return self.status()
            self._current.update(state="finalizing", stop_reason=reason, stopped_at=time.time())
            self._save()
            proc, directory = self._proc, self.root / self._current["id"]
        try:
            forced = False
            if proc.poll() is None:
                # UxPlay sends EOS to its MP4 muxer on SIGINT. Do not SIGKILL normally:
                # an unfinalized MP4 may have no moov atom and cannot be played.
                try:
                    proc.send_signal(signal.SIGINT)
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                forced = True
                proc.kill()
                proc.wait(timeout=5)
            self._logfile.close()
            errors, files = [], []
            if forced:
                errors.append("接收器未正常退出，文件可能不完整。")
            elif proc.returncode:
                errors.append(f"接收器异常退出（{proc.returncode}），请检查 uxplay.log。")
            for index, source in enumerate(sorted(directory.glob("capture.*.mp4")), 1):
                try:
                    files.append(inspect_recording(source, index))
                except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    errors.append(f"{source.name}: {exc}")
            if not files:
                errors.append("没有收到可读取的音视频文件，请确认手机选择了 iPhone Media Test 并播放内容。")
            with self._lock:
                self._current.update(state="ready" if files and not errors else "failed", files=files,
                                     errors=errors, exit_code=proc.returncode, finished_at=time.time())
                self._save()
        except Exception as exc:
            log.exception("Capture finalization failed")
            with self._lock:
                self._current.update(state="failed", errors=[str(exc)], finished_at=time.time())
                self._save()
        finally:
            self._finished.set()
        return self.status()

    def shutdown(self) -> None:
        self.stop("shutdown")
        self._finished.wait(timeout=180)

    def recordings(self) -> list[dict]:
        items = []
        for path in sorted(self.root.glob("*/result.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
            try:
                item = json.loads(path.read_text())
                if item["state"] not in ACTIVE:
                    items.append(item)
            except (OSError, ValueError, KeyError):
                continue
        return items

    def file(self, recording_id: str, name: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", recording_id):
            raise FileNotFoundError
        directory = self.root / recording_id
        manifest = json.loads((directory / "result.json").read_text())
        if manifest["state"] in ACTIVE:
            raise FileNotFoundError
        allowed = {"result.json", "uxplay.log"}
        if manifest.get("timing_log") == "timing.jsonl":
            allowed.add("timing.jsonl")
        for item in manifest["files"]:
            allowed.update(item[key] for key in ("source", "audio", "playback") if key in item)
        path = directory / name
        if name not in allowed or path.resolve().parent != directory.resolve() or not path.is_file():
            raise FileNotFoundError
        return path


def create_app(lab: CaptureLab | None = None) -> FastAPI:
    lab = lab or CaptureLab()

    @asynccontextmanager
    async def lifespan(_):
        yield
        await asyncio.to_thread(lab.shutdown)

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.get("/")
    def index():
        return FileResponse(Path(__file__).parent / "static" / "capture.html")

    @app.get("/api/status")
    def status():
        return lab.status()

    @app.get("/api/recordings")
    def recordings():
        return lab.recordings()

    def local_action(request: Request):
        if request.headers.get("x-capture-lab") != "1":
            raise HTTPException(403, "Open the local capture page to start/stop recording.")

    @app.post("/api/start")
    def start(request: Request):
        local_action(request)
        try:
            return lab.start()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/stop")
    def stop(request: Request):
        local_action(request)
        return lab.stop()

    @app.get("/files/{recording_id}/{name}")
    def file(recording_id: str, name: str):
        try:
            path = lab.file(recording_id, name)
        except (OSError, ValueError, KeyError) as exc:
            raise HTTPException(404, "File not found") from exc
        return FileResponse(path)

    return app


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(), host="127.0.0.1", port=8891)
