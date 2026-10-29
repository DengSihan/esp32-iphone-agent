"""BLE link to the current firmware (docs/ble-protocol.md): the Mac plays the part of the iOS app.

The board is a BLE peripheral named "ESP32 Agent XXXX". Commands go to `cmd` as "<id> <command>" and the reply
echoes the id on `reply`; `status` is a JSON heartbeat every second; `video` carries the mirrored screen as
H.264 chunks (decoded in blescreen.py). The link reconnects on its own, like the old TCP board did.
"""

import asyncio
import json
import logging
import random
import struct
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .device import BoardTimeout, DeviceError, Link

log = logging.getLogger(__name__)

BASE = "6e400001-a9e8-4b1b-9c39-6d6f8a3c"
SVC, CMD, REPLY, STATUS, VIDEO, NET = (BASE + s for s in ("0000", "0001", "0002", "0003", "0004", "0005"))
START, END, IDR, CONFIG, BENCH, SNAP = 1, 2, 4, 8, 16, 32  # video chunk flags
NAMES = ("ESP32 Agent", "iPhone Agent")  # advertised name prefixes (current, older firmware)
TYPE_CHUNK = 160  # characters per `type` command: one write, well under the board's 512-byte line


@dataclass
class AccessUnit:
    data: bytes  # Annex-B
    flags: int
    board_ms: int  # board clock when the board received it from AirPlay

    @property
    def idr(self) -> bool:
        return bool(self.flags & IDR)

    @property
    def config(self) -> bool:
        return bool(self.flags & CONFIG)

    @property
    def snap(self) -> bool:
        return bool(self.flags & SNAP)


class Reassembler:
    """Rebuilds access units from video notifies; after a seq gap waits for the next CONFIG/IDR."""

    def __init__(self):
        self.drops = 0
        self.reset()

    def reset(self) -> None:
        self.expect, self.cur, self.waiting = None, None, False

    def feed(self, data: bytes) -> AccessUnit | None:
        """One notify: `[seq u16 LE][flags u8][payload]`. Returns an access unit when one is complete."""
        if len(data) < 3:
            return None
        seq, flags = struct.unpack_from("<HB", data)
        payload = data[3:]
        if self.expect is not None and seq != self.expect:
            self.drops += (seq - self.expect) & 0xFFFF
            self.cur, self.waiting = None, True
        self.expect = (seq + 1) & 0xFFFF
        if flags & BENCH:
            return None
        if flags & START:
            if (self.waiting and not flags & (CONFIG | IDR)) or len(payload) < 4:
                self.cur = None
                return None
            self.waiting = False
            self.cur = (flags, struct.unpack_from("<I", payload)[0], bytearray(payload[4:]))
        elif self.cur is not None:
            self.cur[2].extend(payload)
        if flags & END and self.cur is not None:
            unit = AccessUnit(bytes(self.cur[2]), self.cur[0] | flags, self.cur[1])
            self.cur = None
            return unit
        return None


class BleBoard(Link):
    """Finds the board, keeps one BLE connection up, and sends it commands one at a time."""

    def __init__(self, name: str = "", address: str = "", scan_timeout: float = 10):
        super().__init__()
        self.name, self.address, self.scan_timeout = name, address, scan_timeout
        self.state = "starting"  # for the UI: scanning, connecting, connected, or the last error
        self.found = ""  # advertised name of the board in use
        self.status: dict = {}
        self.status_at = 0.0
        self.net: dict | None = None  # hotspot settings (`net` read; the first read pairs the Mac)
        self.on_video: Callable[[bytes], None] | None = None
        self.on_status: Callable[[dict], None] | None = None
        self.on_link: Callable[[bool], Awaitable[None]] | None = None  # (re)connected and subscribed / lost
        self.on_timeout: Callable[[str], Awaitable[None]] | None = None  # a command timed out; retried once after
        self._client = None
        self._ready = False
        self._lock = asyncio.Lock()  # CoreBluetooth: one write-with-response at a time
        self._pending: dict[str, asyncio.Future] = {}
        # the board notifies replies to every central; the iOS app uses 100000-800000, board_test.py 900000+
        self._next_id = random.randint(1_000_000, 8_000_000)
        self._task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self._ready and self._client is not None and self._client.is_connected

    def start(self) -> None:
        self._task = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        client, self._client, self._ready = self._client, None, False
        if client is not None and client.is_connected:
            await client.disconnect()

    async def _supervise(self) -> None:
        """Keep the link up: scan, connect, subscribe, wait for a disconnect, start over."""
        while True:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # BleakError, CoreBluetooth errors: retry, never give up
                self.state = f"error: {e}"
                log.warning("BLE: %s", e)
            await self._down()
            await asyncio.sleep(2)

    async def _find(self):
        from bleak import BleakScanner

        self.state = "scanning"
        if self.address:  # on macOS a CoreBluetooth UUID, not a MAC address
            return await BleakScanner.find_device_by_address(self.address, timeout=self.scan_timeout)

        def match(device, adv) -> bool:
            name = device.name or adv.local_name or ""
            return name.startswith(self.name) if self.name else name.startswith(NAMES)

        return await BleakScanner.find_device_by_filter(match, timeout=self.scan_timeout)

    async def _session(self) -> None:
        from bleak import BleakClient

        device = await self._find()
        if device is None:
            self.state = "not found: is the board powered (plugged into the phone)?"
            return
        self.state, self.found = "connecting", device.name or device.address
        loop, lost = asyncio.get_running_loop(), asyncio.Event()
        client = BleakClient(device, disconnected_callback=lambda _: loop.call_soon_threadsafe(lost.set), timeout=20)
        await client.connect()
        try:
            self._client = client
            await client.start_notify(REPLY, self._on_reply)
            await client.start_notify(STATUS, self._on_status)
            await client.start_notify(VIDEO, self._on_video)
            self._ready = True
            self.links += 1
            self._last = self._beat = time.time()  # fresh link: nothing to keep awake yet
            self.state = "connected"
            log.info("board %s connected over BLE (mtu %s)", self.found, client.mtu_size)
            if self.on_link:
                await self._safely(self.on_link(True))
            reading = asyncio.create_task(self._read_net(client))
            await lost.wait()
            reading.cancel()
            log.info("board %s disconnected", self.found)
        finally:
            self._ready, self._client = False, None
            if client.is_connected:
                await client.disconnect()

    async def _down(self) -> None:
        """The link is gone: fail every command in flight."""
        waiting, self._pending = self._pending, {}
        for fut in waiting.values():
            if not fut.done():
                fut.set_exception(DeviceError("board disconnected"))
        if self.on_link and self.links:
            await self._safely(self.on_link(False))

    @staticmethod
    async def _safely(coro) -> None:
        try:
            await coro
        except Exception as e:  # a callback failing must not take the link down
            log.warning("BLE link callback: %s", e)

    async def _read_net(self, client) -> None:
        """The hotspot SSID/password, so the UI can show what the phone joins. Encrypted: the first read
        makes macOS ask to pair."""
        try:
            self.net = json.loads(await asyncio.wait_for(client.read_gatt_char(NET), 90))
            log.info("board network: mode %s, ssid %s", self.net.get("mode"), self.net.get("ssid"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.info("could not read the board's network settings (pairing declined?): %r", e)

    def _on_reply(self, _, data: bytearray) -> None:
        text = bytes(data).decode(errors="replace").strip()
        rid, _, rest = text.partition(" ")
        if rid in ("log", "app"):  # debug lines relayed between the phone's app and a Mac harness
            log.debug("relayed %s", text[:200])
            return
        fut = self._pending.pop(rid, None)
        if fut and not fut.done():
            fut.set_result(rest.strip())

    def _on_status(self, _, data: bytearray) -> None:
        try:
            status = json.loads(bytes(data))
        except ValueError:
            log.debug("bad status json: %r", bytes(data)[:80])
            return
        self.status, self.status_at = status, time.time()
        if self.on_status:
            self.on_status(status)

    def _on_video(self, _, data: bytearray) -> None:
        if self.on_video:
            self.on_video(bytes(data))

    @staticmethod
    def _timeout(command: str) -> float:
        name, _, arg = command.partition(" ")
        if name == "snap":
            return 15  # a whole GOP over BLE
        if name == "bench":
            return 5 + (float(arg) if arg.isdigit() else 0) / 10  # KB at >= 80 kbps
        return Link._timeout(command)

    async def send(self, command: str, timeout: float | None = None) -> str:
        """Send one command; returns the text after "ok" (e.g. coex's mode). Raises DeviceError on err, a lost
        link, or a timeout (BoardTimeout). After a first timeout `on_timeout` runs (it turns the video stream
        off) and the command is tried once more."""
        name, _, text = command.partition(" ")
        if name == "type" and len(text) > TYPE_CHUNK:
            for i in range(0, len(text), TYPE_CHUNK):
                await self.send("type " + text[i:i + TYPE_CHUNK])
            return ""
        try:
            return await self._send(command, timeout)
        except BoardTimeout:
            if name in ("ping", "stream") or not self.on_timeout or not self.connected:
                raise
            await self.on_timeout(command)
            log.info("retrying '%s' after a timeout", command[:40])
            return await self._send(command, timeout)

    async def _send(self, command: str, timeout: float | None) -> str:
        if not self.connected:
            raise DeviceError("board not connected")
        timeout = timeout or self._timeout(command)
        async with self._lock:
            client = self._client
            if not self.connected or client is None:
                raise DeviceError("board not connected")
            rid = str(self._next_id)
            self._next_id += 1
            fut = asyncio.get_running_loop().create_future()
            self._pending[rid] = fut
            started = time.time()
            try:
                await asyncio.wait_for(client.write_gatt_char(CMD, f"{rid} {command}".encode(), response=True),
                                       timeout)
                reply = await asyncio.wait_for(fut, max(0.1, timeout - (time.time() - started)))
            except asyncio.TimeoutError as e:
                if not self.connected:
                    raise DeviceError(f"board link lost during '{command[:30]}'") from e
                raise BoardTimeout(command[:30], timeout) from e
            except DeviceError:
                raise
            except Exception as e:  # BleakError and friends: the link went away mid-command
                raise DeviceError(f"board link lost during '{command[:30]}': {e!r}") from e
            finally:
                self._pending.pop(rid, None)
        self._replied(command)
        if command != "ping":
            log.debug("%s -> %s (%.0f ms)", command[:60], reply[:60], (time.time() - started) * 1000)
        if reply == "ok" or reply.startswith("ok "):
            return reply[3:]
        raise DeviceError(reply or "board disconnected")
