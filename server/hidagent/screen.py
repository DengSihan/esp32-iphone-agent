"""The phone's screen, received over AirPlay mirroring by UxPlay, as JPEG frames."""

import asyncio
import logging
import os
import shutil
import subprocess
import threading
import time

log = logging.getLogger(__name__)


def uxplay() -> str:
    """The UxPlay binary: on PATH, or where a build or Homebrew puts it (a shell may not have those on its PATH)."""
    places = (os.path.expanduser("~/.local/bin/uxplay"), "/opt/homebrew/bin/uxplay", "/usr/local/bin/uxplay")
    return shutil.which("uxplay") or next((p for p in places if os.path.exists(p)), "uxplay")


class Screen:
    """Runs UxPlay with a GStreamer sink that writes JPEGs into a pipe; keeps the latest one."""

    def __init__(self, name: str = "iPhone Agent", fps: int = 8, quality: int = 75, port: int | None = None):
        """port: UxPlay's first TCP/UDP port (it uses port..port+2) instead of 7100/7000/7001, e.g. when macOS's own
        AirPlay Receiver already holds 7000."""
        self.name, self.fps, self.quality, self.port = name, fps, quality, port
        self.frame: bytes | None = None
        self.updated = 0.0
        self._proc = None
        self._clients_at, self._clients_n = 0.0, None
        self._stopping = False

    def start(self) -> None:
        threading.Thread(target=self._supervise, daemon=True).start()

    def stop(self) -> None:
        self._stopping = True
        if self._proc:
            self._proc.terminate()

    def _supervise(self) -> None:
        """Keep UxPlay running: it exits on some disconnects, so restart it every time."""
        while not self._stopping:
            read_fd, write_fd = os.pipe()
            # every decoded frame becomes a JPEG (AirPlay sends frames only when the screen changes). No videorate:
            # with -vsync no UxPlay leaves buffers without timestamps, and videorate then passes only the first one
            sink = f"jpegenc quality={self.quality} ! fdsink fd={write_fd}"
            ports = ["-p", str(self.port)] if self.port else []
            self._proc = subprocess.Popen(
                # -vsync no: show each frame as it arrives (live streaming), not on audio-sync timestamps
                [uxplay(), "-n", self.name, "-nh", "-a", "-reset", "0", "-vsync", "no", *ports, "-vs", sink],
                pass_fds=(write_fd,))  # UxPlay logs to stdout: keep them in ours
            os.close(write_fd)
            log.info("AirPlay receiver '%s' started (pid %d)", self.name, self._proc.pid)
            self._read(read_fd)  # returns when UxPlay closes the pipe
            code = self._proc.wait()
            self.frame = None
            log.warning("AirPlay receiver exited (code %s), restarting", code)
            time.sleep(1)

    @property
    def live(self) -> bool:
        running = self._proc is not None and self._proc.poll() is None
        if not running or self.frame is None:
            return False
        clients = self._clients()
        if clients is not None:  # is a phone connected to UxPlay right now?
            return clients > 0
        return time.time() - self.updated < 90  # unknown: AirPlay sends no frames while the screen is still

    def _clients(self) -> int | None:
        """Established TCP connections of the UxPlay process (a mirroring phone keeps some open).

        Read from /proc (Linux) or lsof (macOS), cached for a second; None when neither works.
        """
        if time.time() - self._clients_at < 1:
            return self._clients_n
        if not os.path.isdir("/proc"):
            n = self._lsof_clients()
            self._clients_at, self._clients_n = time.time(), n
            return n
        try:
            fds = os.listdir(f"/proc/{self._proc.pid}/fd")
            inodes = set()
            for fd in fds:
                link = os.readlink(f"/proc/{self._proc.pid}/fd/{fd}")
                if link.startswith("socket:["):
                    inodes.add(link[8:-1])
            n = 0
            for table in ("/proc/net/tcp", "/proc/net/tcp6"):
                with open(table) as f:
                    for row in f.readlines()[1:]:
                        cols = row.split()
                        n += cols[3] == "01" and cols[9] in inodes  # 01 = ESTABLISHED
        except OSError:
            n = None
        self._clients_at, self._clients_n = time.time(), n
        return n

    def _lsof_clients(self) -> int | None:
        try:
            out = subprocess.run(["lsof", "-nP", "-a", "-p", str(self._proc.pid), "-iTCP", "-sTCP:ESTABLISHED", "-Fn"],
                                 capture_output=True, text=True, timeout=2).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        return sum(line.startswith("n") for line in out.splitlines())

    @property
    def aspect(self) -> float:
        """Frame width / height, read from the JPEG header (portrait iPhone as a fallback)."""
        f = self.frame or b""
        i = f.find(b"\xff\xc0")  # baseline start-of-frame: ... height(2) width(2)
        if i < 0 or len(f) < i + 9:
            return 9 / 19.5
        return int.from_bytes(f[i + 7:i + 9], "big") / int.from_bytes(f[i + 5:i + 7], "big")

    async def settled(self, quiet: float = 0.35, least: float = 0.25, most: float = 4) -> None:
        """Wait until the screen stops changing (AirPlay only sends frames on change)."""
        started = time.time()
        await asyncio.sleep(least)
        while time.time() - self.updated < quiet and time.time() - started < most:
            await asyncio.sleep(0.1)

    async def prepare(self) -> str:
        """Start of a run: UxPlay's frames need no setup (blescreen.BleScreen turns its stream on here)."""
        return "live"

    async def after_action(self, since: float) -> None:
        """After a gesture that ended at `since`: wait until the screen shows its result."""
        await self.settled()

    async def fresh(self, since: float, timeout: float = 0.8) -> bytes | None:
        """The first frame received after `since`, or None if none arrives in time."""
        deadline = time.time() + timeout
        while self.updated <= since:
            if time.time() > deadline:
                return None
            await asyncio.sleep(0.02)
        return self.frame

    def _read(self, fd: int) -> None:
        buf = b""
        with os.fdopen(fd, "rb", buffering=0) as pipe:
            while chunk := pipe.read(1 << 16):
                buf += chunk
                while (start := buf.find(b"\xff\xd8")) >= 0 and (end := buf.find(b"\xff\xd9", start)) >= 0:
                    self.frame, self.updated = buf[start:end + 2], time.time()
                    buf = buf[end + 2:]
