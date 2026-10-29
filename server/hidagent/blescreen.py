"""The phone's screen as the current firmware delivers it: AirPlay -> board -> BLE `video` -> H.264 -> JPEG.

Port of the iOS app's VideoFeed. Same interface as screen.Screen (frame, updated, live, aspect, settled, fresh),
plus what a slow, lossy link needs: capture times from the board's clock, the live stream (`stream on`) with a
`snap` of the board's buffered GOP as the fallback, and after_action() so the model never sees a frame from
before its last action.
"""

import asyncio
import io
import logging
import queue
import threading
import time
from collections import deque
from typing import NamedTuple

import av

from .ble import AccessUnit, BleBoard, Reassembler
from .device import DeviceError

log = logging.getLogger(__name__)


class Frame(NamedTuple):
    image: object  # PIL.Image
    updated: float  # local time it was decoded
    captured_at: float  # when the board got it, in local time (≈)


class BleScreen:
    """Decodes the board's video on a worker thread; keeps the newest frame (JPEG made on first use)."""

    def __init__(self, board: BleBoard, mode: str = "stream", quality: int = 80):
        self.board, self.mode, self.quality = board, mode, quality
        self.fallback: str | None = None  # "snap" after a command timeout, for the rest of the run
        self.stream_on = False
        self.broken = False  # a lost chunk or decode error since the last good frame: stale until the next IDR
        self.note = ""  # last decoder problem, for the status page
        self.decoded = 0
        self._latest: Frame | None = None
        self._jpeg: tuple[Frame, bytes] | None = None
        self._clock: float | None = None  # local s - board s, the smallest seen (≈ the link latency)
        self._assembler = Reassembler()
        self._units: queue.Queue[AccessUnit | None] = queue.Queue()  # None = start over (new link)
        self._bytes: deque[tuple[float, int]] = deque()  # (time, bytes) of the last 5 s of notifies, for the rate
        board.on_video, board.on_status = self._receive, self._status
        board.on_link, board.on_timeout = self._link, self._timed_out
        threading.Thread(target=self._decode_loop, name="h264", daemon=True).start()

    # ---- what the agent and the pointer read --------------------------------------------------------------

    @property
    def mirroring(self) -> bool:
        return self.board.status.get("ap") == "streaming"

    @property
    def live(self) -> bool:
        return self.board.connected and self.mirroring and self._latest is not None

    @property
    def frame(self) -> bytes | None:
        latest = self._latest
        if latest is None:
            return None
        cached = self._jpeg
        if cached is None or cached[0] is not latest:
            buf = io.BytesIO()
            latest.image.save(buf, "JPEG", quality=self.quality)
            cached = self._jpeg = (latest, buf.getvalue())
        return cached[1]

    @property
    def updated(self) -> float:
        return self._latest.updated if self._latest else 0.0

    @property
    def captured_at(self) -> float:
        return self._latest.captured_at if self._latest else 0.0

    @property
    def aspect(self) -> float:
        """Frame width / height (portrait iPhone as a fallback)."""
        if self._latest is None:
            return 9 / 19.5
        w, h = self._latest.image.size
        return w / h

    @property
    def size(self) -> tuple[int, int]:
        return self._latest.image.size if self._latest else (0, 0)

    @property
    def lost(self) -> int:
        """Video chunks lost on the link since start (seq gaps)."""
        return self._assembler.drops

    @property
    def kbps(self) -> float:
        """Video bytes received over BLE in the last 5 s."""
        now = time.time()
        recent = [n for t, n in list(self._bytes) if now - t < 5]
        return sum(recent) * 8 / 1000 / 5

    @property
    def frame_mode(self) -> str:
        return self.fallback or self.mode

    def _lag(self) -> float:
        """How late the shown frame arrived after the board got it, capped (0 when unknown)."""
        f = self._latest
        return min(max(f.updated - f.captured_at, 0), 1) if f and f.captured_at > 0 else 0

    async def settled(self, quiet: float = 0.5, least: float = 0.3, most: float = 4) -> None:
        """Wait until the screen stops changing (AirPlay only sends frames on change). Mid-animation frames are
        big and reach us late over BLE, so a gap is only "quiet" once it also covers the last frame's lag."""
        started = time.time()
        await asyncio.sleep(least)
        while time.time() - self.updated < quiet + self._lag() and time.time() - started < most:
            await asyncio.sleep(0.1)

    async def fresh(self, since: float, timeout: float = 0.8) -> bytes | None:
        """The first frame captured after `since`, or None if none arrives in time."""
        deadline = time.time() + timeout
        while (self.captured_at or self.updated) <= since:
            if time.time() > deadline:
                return None
            await asyncio.sleep(0.02)
        return self.frame

    async def _newer(self, since: float, timeout: float) -> bool:
        deadline = time.time() + timeout
        while self.captured_at < since:
            if time.time() > deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    # ---- run control ------------------------------------------------------------------------------------

    async def prepare(self) -> str:
        """Start of a run: stream again (a timeout fallback lasts one run), and make sure there is a frame."""
        self.fallback = None
        if self.mode == "stream" and not self.stream_on:
            await self.stream(True)
        elif self.mode == "snap" and self.stream_on:
            await self.stream(False)
        if self.stream_on:
            await self.fresh(time.time(), timeout=2)  # let a moving screen show up first
            await self.settled()
        return await self.current_frame()

    async def current_frame(self, max_age: float = 1, wait: float = 3) -> str:
        """A current frame for the model: from the live stream if it has one younger than max_age (or one comes
        within `wait`), else a snap, retried once with a longer timeout. Returns where it came from."""
        if self.stream_on and self.mirroring:
            if self._latest and time.time() - self.updated < max_age and not self.broken:
                return "live"
            if await self.fresh(time.time() - max_age, timeout=wait) is not None:
                return "live (waited)"
            if self._latest and not self.broken:
                return "live (still screen)"
        try:
            await self.snap()
        except DeviceError as e:
            if "did not answer" not in str(e):
                raise
            log.info("snap timed out; retrying once with a longer timeout")
            await self.snap(self.snap_timeout * 1.5)
        return "snap"

    async def after_action(self, since: float) -> None:
        """The model must never see a frame from before the action that ended at `since`."""
        try:
            if self.frame_mode == "snap":
                await asyncio.sleep(0.7)
                await self.current_frame()
                if self.captured_at < since:
                    log.debug("frame: snap ends %.1f s before the action; snapping again", since - self.captured_at)
                    await asyncio.sleep(0.5)
                    await self.current_frame()
            else:
                await self.settled()
                if await self._newer(since, 1.5):
                    return
                if self.stream_on and not self.broken:
                    return  # a healthy stream sent nothing new: the screen did not change
                log.debug("frame: stream stale (waiting for an IDR); snapping")
                await self.snap()
        except DeviceError as e:
            log.info("frame after action failed: %s", e)

    @property
    def snap_timeout(self) -> float:
        """Seconds a snap may take: the buffered GOP over the measured BLE rate, doubled, at least 8 s."""
        gop = float(self.board.status.get("gop", 0) or 0)
        rate = max(self.kbps, float((self.board.status.get("ble") or {}).get("kbps", 0) or 0), 40)
        return max(8.0, gop * 8 / 1000 / rate * 2 + 3)

    async def snap(self, timeout: float | None = None) -> float:
        """Ask the board for its current GOP and wait until it is decoded. Returns the latency in ms."""
        started = time.time()
        await self.board.send("snap", timeout=timeout or self.snap_timeout)
        await asyncio.to_thread(self._units.join)  # the whole GOP is decoded
        return (time.time() - started) * 1000

    async def stream(self, on: bool) -> None:
        await self.board.send(f"stream {'on' if on else 'off'}")
        self.stream_on = on

    # ---- board callbacks ------------------------------------------------------------------------------

    async def _link(self, up: bool) -> None:
        self.stream_on = False  # the board forgets `stream on` with the link
        if not up:
            return
        self._assembler.reset()  # chunk numbering starts over
        self._clock = None  # the board may have rebooted: its clock too
        self._units.put(None)
        if self.mode == "stream":
            await self.stream(True)

    async def _timed_out(self, command: str) -> None:
        """A command timed out: video may be starving the board's control traffic. Snap per step for the rest
        of this run (the next run streams again)."""
        log.info("command '%s' timed out: stream off, snap per step for this run", command[:30])
        self.fallback = "snap"
        if self.stream_on:
            try:
                await self.stream(False)
            except DeviceError as e:
                log.info("stream off failed: %s", e)

    def _status(self, status: dict) -> None:
        if "up" in status:
            self._board_clock(int(status["up"]))

    def _board_clock(self, ms: int) -> None:
        offset = time.time() - ms / 1000
        self._clock = offset if self._clock is None else min(self._clock, offset)

    def _receive(self, chunk: bytes) -> None:
        now = time.time()
        self._bytes.append((now, len(chunk)))
        while now - self._bytes[0][0] > 5:
            self._bytes.popleft()
        drops = self._assembler.drops
        unit = self._assembler.feed(chunk)
        if self._assembler.drops > drops:
            self.broken = True
        if unit is None:
            return
        if not unit.snap:  # snap units are resent late: their ages say nothing about the link
            self._board_clock(unit.board_ms)
        self._units.put(unit)

    # ---- decoding (worker thread) ---------------------------------------------------------------------

    def _decode_loop(self) -> None:
        codec, config, pending = None, b"", None  # pending: the newest decoded frame, not yet shown
        while True:
            unit = self._units.get()
            try:
                if unit is None:
                    codec, config, pending = None, b"", None
                elif unit.config:
                    config = unit.data  # SPS + PPS, sent before each IDR
                elif codec is not None or unit.idr:  # nothing to decode against until an IDR
                    codec = codec or av.CodecContext.create("h264", "r")
                    frames = codec.decode(av.Packet((config if unit.idr else b"") + unit.data))
                    self.decoded += len(frames)
                    pending = (frames[-1], unit) if frames else pending
            except Exception as e:  # a corrupt unit: keep showing the old frame until the next IDR
                self.note, self.broken = f"decode: {e}", True
            finally:
                if pending and self._units.empty():  # only the newest frame becomes an image
                    self._publish(*pending)
                    pending = None
                self._units.task_done()

    def _publish(self, frame, unit: AccessUnit) -> None:
        now = time.time()
        captured = unit.board_ms / 1000 + self._clock if self._clock is not None else now
        self._latest = Frame(frame.to_image(), now, min(captured, now))
        self.broken = False
