"""Capture decoded AirPlay media on its shared presentation clock, never arrival time."""

import io
import json
import queue
import shutil
import threading
import time
import uuid
import wave
from fractions import Fraction
from pathlib import Path

import av
from PIL import Image, ImageOps

from .capture import CaptureLab, inspect_recording, run_media
from .media_transport import MediaPacket

NANOSECOND = 1_000_000_000
MAX_QUEUED_BYTES = 128 * 1024 * 1024


class StreamRecorder:
    def __init__(self, root: Path | None = None, max_seconds: int = 180, fps: int = 30, live=None,
                 clock_ns=time.time_ns):
        self.root = root or Path(__file__).resolve().parents[1] / "out" / "recordings"
        self.max_seconds, self.fps, self.live = max_seconds, fps, live
        self._clock_ns = clock_ns
        self._lock = threading.RLock()
        self._current = None
        self._jpeg = None
        self._video_pts = None
        self._audio_at = 0.0
        self._accepting = False
        self._end = threading.Event()
        self._finished = threading.Event()
        self._finished.set()

    def video(self, jpeg: bytes, pts_ns: int | None = None) -> None:
        with self._lock:
            self._jpeg, self._video_pts = jpeg, pts_ns
            self._submit(MediaPacket(2, pts_ns, 0, 0, 0, 0, False, jpeg))

    def audio(self, pcm: bytes, pts_ns: int | None, rate: int, channels: int,
              discontinuity: bool = False) -> None:
        with self._lock:
            self._audio_at = time.monotonic()
            self._submit(MediaPacket(1, pts_ns, 0, rate, channels, 0, discontinuity, pcm))

    def _submit(self, packet: MediaPacket) -> None:
        if not self._accepting:
            return
        if packet.pts_ns is None:
            self.fail("接收流缺少媒体时间戳，已停止采集，不能按到达时间猜测同步。")
            return
        if self._queued_bytes + len(packet.data) > MAX_QUEUED_BYTES:
            self.fail("录制写入跟不上接收速度，已停止并保留部分文件。")
            return
        self._queued_bytes += len(packet.data)
        self._queue.put(packet)

    def fail(self, message: str) -> None:
        with self._lock:
            if self._accepting:
                self._current["errors"].append(message)
                self.stop("stream_error")
                self._accepting = False

    def status(self) -> dict:
        with self._lock:
            c = dict(self._current) if self._current else None
            if c and c["state"] == "recording":
                c["elapsed_seconds"] = round(time.monotonic() - self._origin_mono, 1)
            return {"current": c, "max_seconds": self.max_seconds, "version": 2,
                    "timing": "source_pts",
                    "audio_recent": bool(self._audio_at and time.monotonic() - self._audio_at < 2)}

    def _save(self):
        path = self.root / self._current["id"] / "result.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._current, ensure_ascii=False, indent=2))
        temporary.replace(path)

    def start(self, task: str = "") -> dict:
        with self._lock:
            if not self._finished.is_set():
                raise RuntimeError("正在采集或保存，请等待上一段结束。")
            if not self._jpeg or self._video_pts is None:
                raise RuntimeError("还没有带时间戳的手机画面，请重启服务并重新连接 iPhone Agent。")
            if not all(shutil.which(name) for name in ("ffmpeg", "ffprobe")):
                raise RuntimeError("需要 ffmpeg 和 ffprobe 才能保存音视频。")
            recording_id = uuid.uuid4().hex
            directory = self.root / recording_id
            directory.mkdir(parents=True, mode=0o700)
            self._origin_ns, self._origin_mono = self._clock_ns(), time.monotonic()
            self._current = {"id": recording_id, "state": "recording", "started_at": self._origin_ns / NANOSECOND,
                             "task": task, "files": [], "errors": [], "timing": "source_pts",
                             "source": "decoded_airplay", "version": 2, "stop_reason": None,
                             "audio_received_seconds": 0, "audio_format": None,
                             "source_discontinuities": 0, "timing_log": "timing.jsonl"}
            self._queue = queue.Queue()
            self._queued_bytes = 0
            self._end.clear()
            self._finished.clear()
            self._accepting = True
            seed = self._jpeg
            seed_pts = self._video_pts
            if seed_pts > self._origin_ns:
                self._submit(MediaPacket(2, seed_pts, 0, 0, 0, 0, False, seed))
            # A static screen needs a frame at capture start, even if AirPlay sends no new frame.
            self._save()
            threading.Thread(target=self._record, args=(directory, seed, seed_pts), daemon=True).start()
            return self.status()

    def stop(self, reason: str = "manual") -> dict:
        with self._lock:
            if self._current and self._current["state"] == "recording":
                self._stop_ns = min(self._clock_ns(), self._origin_ns + self.max_seconds * NANOSECOND)
                self._stopped_mono = time.monotonic()
                self._current.update(state="finalizing", stop_reason=reason)
                self._end.set()
            return self.status()

    def _record(self, directory: Path, seed: bytes, seed_pts: int) -> None:
        container = None
        rate = channels = samples = received = gaps = boundary_frames = 0
        audio_ranges = []
        count, last_pts = 0, -1
        try:
            with Image.open(io.BytesIO(seed)) as first:
                size = (first.width // 2 * 2, first.height // 2 * 2)
            container = av.open(str(directory / "video.mp4"), "w")
            stream = container.add_stream("libx264", rate=self.fps)
            stream.width, stream.height = size
            stream.pix_fmt = "yuv420p"
            stream.time_base = stream.codec_context.time_base = Fraction(1, 1_000_000)
            stream.options = {"preset": "ultrafast", "crf": "20", "bf": "0"}
            pixels = Image.new("RGB", size, "black")

            def frame(jpeg, pts):
                nonlocal count, last_pts, pixels
                if jpeg is not None:
                    with Image.open(io.BytesIO(jpeg)) as image:
                        pixels = ImageOps.pad(image.convert("RGB"), size, color="black")
                video = av.VideoFrame.from_image(pixels)
                video.time_base, video.pts = Fraction(1, 1_000_000), pts
                for packet in stream.encode(video):
                    container.mux(packet)
                count += 1
                last_pts = pts

            # A prefetched future picture must not appear ahead of its audio.
            frame(seed if seed_pts <= self._origin_ns else None, 0)
            connection_check = time.monotonic() + 1
            with (directory / "received.pcm").open("w+b") as pcm, (directory / "timing.jsonl").open("w") as timing:
                while True:
                    now = time.monotonic()
                    if not self._end.is_set():
                        if now - self._origin_mono >= self.max_seconds:
                            self.stop("time_limit")
                        elif self.live and now >= connection_check:
                            connection_check = now + 1
                            if not self.live():
                                self.stop("connection_lost")
                    # Drain already received data and allow in-flight pipe buffers to arrive.
                    with self._lock:
                        if self._end.is_set() and now - self._stopped_mono >= .25 and self._queue.empty():
                            self._accepting = False
                            break
                    try:
                        packet = self._queue.get(timeout=.02)
                    except queue.Empty:
                        continue
                    with self._lock:
                        self._queued_bytes -= len(packet.data)
                    relative = packet.pts_ns - self._origin_ns
                    limit = (self._stop_ns - self._origin_ns) if self._end.is_set() else self.max_seconds * NANOSECOND
                    event = {"kind": packet.kind, "pts_ns": packet.pts_ns, "relative_ns": relative,
                             "bytes": len(packet.data), "discontinuity": packet.discontinuity}
                    if packet.kind == 2:
                        pts = round(relative / 1000)
                        if 0 < relative < limit:
                            if pts < last_pts - 2000:
                                raise ValueError("视频时间戳倒退，采集需核验。")
                            if pts > last_pts:
                                frame(packet.data, pts)
                    else:
                        if not 1 <= packet.rate <= 384000 or not 1 <= packet.channels <= 8:
                            raise ValueError("Invalid audio format")
                        if rate and (packet.rate, packet.channels) != (rate, channels):
                            raise ValueError("采集中音频格式发生变化，请重新开始一段采集。")
                        rate, channels = packet.rate, packet.channels
                        width = channels * 2
                        if len(packet.data) % width:
                            raise ValueError("Incomplete PCM sample frame")
                        start = round(relative * rate / NANOSECOND)
                        data = packet.data
                        trim = min(len(data) // width, max(0, -start))
                        data, start = data[trim * width:], max(0, start)
                        maximum = max(0, round(limit * rate / NANOSECOND))
                        keep = max(0, min(len(data) // width, maximum - start))
                        boundary_frames += trim + len(data) // width - keep
                        data = data[:keep * width]
                        if data:
                            # Tiny rounding / decoder timestamp jitter is not a real pause.
                            if samples and abs(start - samples) <= max(2, round(rate * .002)):
                                start = samples
                            if start < samples:
                                raise ValueError("音频时间戳重叠或倒退，停止采集以避免覆盖语音。")
                            gap = start - samples
                            pcm.seek(start * width)
                            pcm.write(data)
                            samples = start + len(data) // width
                            audio_ranges.append((start, samples))
                            received += len(data) // width
                            gaps += gap
                            event.update(sample_start=start, frames=len(data) // width, gap_frames=gap)
                        with self._lock:
                            self._current.update(audio_received_seconds=round(received / rate, 3),
                                                 audio_format={"sample_rate": rate, "channels": channels,
                                                               "sample_format": "s16le"})
                            self._current["source_discontinuities"] += int(packet.discontinuity)
                    timing.write(json.dumps(event) + "\n")
                duration = max(.001, (self._stop_ns - self._origin_ns) / NANOSECOND)
                # Hold a static final picture until the end, on the SAME clock as audio.
                end_pts = round(duration * 1_000_000) - 1
                if end_pts > last_pts:
                    frame(None, end_pts)
                for packet in stream.encode():
                    container.mux(packet)
                container.close()
                container = None
                if received:
                    # Clip only at the explicit capture boundary, never at packet arrival time.
                    length = round(duration * rate)
                    retained = sum(max(0, min(b, length) - a) for a, b in audio_ranges)
                    boundary_frames += received - retained
                    pcm.truncate(length * channels * 2)
                    pcm.seek(0)
                    with wave.open(str(directory / "received.wav"), "wb") as wav:
                        wav.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
                        while chunk := pcm.read(1 << 20):
                            wav.writeframesraw(chunk)
                with self._lock:
                    self._current.update(duration_seconds=duration, stopped_at=self._stop_ns / NANOSECOND,
                                         frames=count, audio_gap_frames=gaps, audio_received_frames=received,
                                         audio_written_frames=retained if received else 0,
                                         audio_received_seconds=round(retained / rate, 3) if received else 0,
                                         boundary_trimmed_frames=boundary_frames)
                    self._save()
            source = directory / "capture.mp4"
            args = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(directory / "video.mp4")]
            if received:
                # Lossless master. AAC is encoded ONCE, for browser playback only.
                args += ["-i", str(directory / "received.wav"), "-map", "0:v:0", "-map", "1:a:0", "-c:a", "alac"]
            else:
                args += ["-map", "0:v:0"]
            args += ["-c:v", "copy", "-t", str(duration), "-movflags", "+faststart", str(source)]
            run_media(args)
            item = inspect_recording(source, 1)
            item["audio_quality"] = "native_rate_channels_pcm16"
            with self._lock:
                self._current["files"] = [item]
                if self._current["stop_reason"] in ("connection_lost", "agent_cancelled", "shutdown", "stream_error"):
                    self._current["errors"].append("采集中断；已保存收到的部分，不能视为完整消息。")
                self._current["state"] = "interrupted" if self._current["errors"] else "ready"
        except Exception as exc:
            with self._lock:
                self._current.update(state="failed", errors=[*self._current["errors"], str(exc)])
        finally:
            try:
                if container:
                    container.close()
            finally:
                with self._lock:
                    self._accepting = False
                    self._current["finished_at"] = time.time()
                    self._save()
                    self._finished.set()

    def wait(self, timeout: int = 150) -> dict:
        if not self._finished.wait(timeout):
            raise RuntimeError("录制文件仍在保存，请稍后查看采集结果。")
        return self.status()

    def recordings(self) -> list[dict]:
        return [r for r in CaptureLab(self.root).recordings() if r["state"] != "recording"]

    def file(self, recording_id: str, name: str) -> Path:
        result = CaptureLab(self.root).file(recording_id, name)
        manifest = json.loads((result.parent / "result.json").read_text())
        if manifest["state"] == "recording":
            raise FileNotFoundError
        return result
