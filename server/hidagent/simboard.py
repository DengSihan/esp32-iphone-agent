"""A simulated board and phone (LINK=sim): exercises the BLE relay end to end without hardware.

SimBoard is a BleBoard whose "BLE client" lives in this process: commands go through the same request ids,
replies and timeouts, and the screen arrives as H.264 access units chunked exactly as the firmware does
(docs/ble-protocol.md), paced to a Mac-like BLE rate. SimPhone draws a small fake iPhone (home screen, Settings,
Notes, Clock) with an AssistiveTouch pointer driven by the mouse commands, so calibration, taps, swipes, typing
and the agent loop can all be tried in the web UI.
"""

import asyncio
import json
import logging
import struct
import time
from fractions import Fraction

import av
from PIL import Image, ImageDraw, ImageFont

from . import ble
from .ble import BleBoard

log = logging.getLogger(__name__)

W, H = 480, 1040  # what the board asks AirPlay for by default
SPAN = (400, 860)  # mouse units across the screen's width / height (iOS pointer speed)
OFFSET = (0.012, 0.008)  # where mouse units 0, 0 land (the corner is not exactly 0, 0)
POINTER_R = 10
APPS = ["Settings", "Notes", "Clock", "Photos", "Maps", "Mail", "Music", "Calendar"]
COLORS = {"Settings": (142, 142, 147), "Notes": (255, 204, 0), "Clock": (30, 30, 30), "Photos": (255, 99, 71),
          "Maps": (52, 199, 89), "Mail": (0, 122, 255), "Music": (255, 45, 85), "Calendar": (255, 59, 48)}
SETTINGS = ["Wi-Fi", "Bluetooth", "Cellular", "Notifications", "Sounds", "Display & Brightness", "General"]


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


class SimPhone:
    """The fake phone's state and drawing. Positions are screen fractions (0..1)."""

    def __init__(self):
        self.ux, self.uy = 0.0, 0.0  # pointer in mouse units
        self.pressed_at: tuple[float, float] | None = None
        self.screen = "home"  # home | Settings | Wi-Fi | Notes | Clock | <other app>
        self.history: list[str] = []
        self.note, self.typing = "", False
        self.wifi = True
        self.taps: list[tuple[float, float]] = []

    @property
    def pointer(self) -> tuple[float, float]:
        return (min(max(OFFSET[0] + self.ux / SPAN[0], 0), 0.999), min(max(OFFSET[1] + self.uy / SPAN[1], 0), 0.999))

    def move(self, dx: int, dy: int) -> None:
        # the phone clamps the pointer at the screen edges, so `home` (a big move up-left) lands in the corner
        self.ux = min(max(self.ux + dx, -OFFSET[0] * SPAN[0]), SPAN[0] * (1 - OFFSET[0]))
        self.uy = min(max(self.uy + dy, -OFFSET[1] * SPAN[1]), SPAN[1] * (1 - OFFSET[1]))

    def press(self) -> None:
        self.pressed_at = self.pointer

    def release(self) -> None:
        if self.pressed_at is None:
            return
        (x1, y1), (x2, y2) = self.pressed_at, self.pointer
        self.pressed_at = None
        if abs(x2 - x1) < 0.02 and abs(y2 - y1) < 0.02:
            self.tap(x2, y2)
        elif y1 > 0.95 and y1 - y2 > 0.2:
            self.go("home")  # the flick up from the home bar
        elif x1 < 0.05 and x2 - x1 > 0.3:
            self.back()

    def go(self, screen: str) -> None:
        if screen == "home":
            self.history = []
        elif self.screen != screen:
            self.history.append(self.screen)
        self.screen, self.typing = screen, False

    def back(self) -> None:
        if self.history:
            self.screen, self.typing = self.history.pop(), False

    def tap(self, x: float, y: float) -> None:
        self.taps.append((x, y))
        if self.screen == "home":
            for name, (left, top, right, bottom) in self._icons():
                if left <= x * W <= right and top <= y * H <= bottom:
                    self.go(name)
        elif self.screen in ("Settings", "Wi-Fi", "Notes") and y * H < 120 and x < 0.3:
            self.back()  # "< Back"
        elif self.screen == "Settings":
            i = int((y * H - 160) // 64)
            if 0 <= i < len(SETTINGS) and y * H >= 160 and SETTINGS[i] == "Wi-Fi":
                self.go("Wi-Fi")
        elif self.screen == "Wi-Fi" and 160 <= y * H <= 224 and x > 0.75:
            self.wifi = not self.wifi
        elif self.screen == "Notes":
            self.typing = 140 <= y * H <= 600

    def type(self, text: str) -> None:
        if self.typing:
            self.note += text

    def key(self, combo: str) -> None:
        if combo == "cmd+h":
            self.go("home")
        elif self.typing and combo == "backspace":
            self.note = self.note[:-1]
        elif self.typing and combo == "enter":
            self.note += "\n"

    @staticmethod
    def _icons():
        size, gap = 84, (W - 4 * 84) / 5
        for i, name in enumerate(APPS):
            col, row = i % 4, i // 4
            left, top = gap + col * (size + gap), 120 + row * (size + 50)
            yield name, (left, top, left + size, top + size)

    def render(self) -> Image.Image:
        img = Image.new("RGB", (W, H), (255, 255, 255))
        d = ImageDraw.Draw(img)
        small, mid, big = _font(16), _font(22), _font(34)
        dark = self.screen in ("home", "Clock")
        if dark:
            d.rectangle((0, 0, W, H), fill=(28, 52, 92) if self.screen == "home" else (0, 0, 0))
        ink = (255, 255, 255) if dark else (0, 0, 0)
        d.text((40, 14), "9:41", font=mid, fill=ink)  # status bar
        if self.screen == "home":
            for name, box in self._icons():
                d.rounded_rectangle(box, 18, fill=COLORS[name])
                d.text(((box[0] + box[2]) / 2, box[3] + 8), name, font=small, fill=ink, anchor="mt")
            d.rounded_rectangle((W / 2 - 50, 880, W / 2 + 50, 912), 16, fill=(70, 90, 130))
            d.text((W / 2, 896), "Search", font=small, fill=ink, anchor="mm")
            d.rounded_rectangle((16, 930, W - 16, 1020), 30, fill=(60, 80, 120))
        elif self.screen == "Settings":
            d.text((20, 70), "< Back", font=mid, fill=(0, 122, 255))
            d.text((20, 100), "Settings", font=big, fill=ink)
            for i, row in enumerate(SETTINGS):
                top = 160 + i * 64
                d.line((20, top + 63, W, top + 63), fill=(220, 220, 220))
                d.text((24, top + 20), row, font=mid, fill=ink)
                d.text((W - 30, top + 20), ">", font=mid, fill=(180, 180, 180))
        elif self.screen == "Wi-Fi":
            d.text((20, 70), "< Back", font=mid, fill=(0, 122, 255))
            d.text((20, 100), "Wi-Fi", font=big, fill=ink)
            d.text((24, 180), "Wi-Fi", font=mid, fill=ink)
            on = self.wifi
            d.rounded_rectangle((W - 100, 172, W - 30, 212), 20, fill=(52, 199, 89) if on else (220, 220, 220))
            d.ellipse((W - 68, 174, W - 32, 210) if on else (W - 98, 174, W - 62, 210), fill=(255, 255, 255))
        elif self.screen == "Notes":
            d.text((20, 70), "< Back", font=mid, fill=(255, 159, 10))
            d.text((20, 100), "Notes", font=big, fill=ink)
            d.rectangle((20, 150, W - 20, 590), outline=(255, 159, 10) if self.typing else (220, 220, 220), width=2)
            d.multiline_text((30, 160), self.note or ("" if self.typing else "Tap to write"), font=mid,
                             fill=ink if self.note else (170, 170, 170))
            if self.typing:  # the on-screen keyboard
                d.rectangle((0, 700, W, H), fill=(210, 212, 218))
                for r, row in enumerate(("qwertyuiop", "asdfghjkl", "zxcvbnm")):
                    for c, ch in enumerate(row):
                        x = 8 + c * 46.5 + r * 20
                        d.rounded_rectangle((x, 720 + r * 70, x + 40, 776 + r * 70), 6, fill=(255, 255, 255))
                        d.text((x + 20, 748 + r * 70), ch, font=mid, fill=(0, 0, 0), anchor="mm")
        else:
            d.text((W / 2, H / 2 - 40), self.screen, font=big, fill=ink, anchor="mm")
            d.text((W / 2, H / 2 + 10), "10:09:30" if self.screen == "Clock" else "(empty app)", font=mid,
                   fill=ink, anchor="mm")
        if self.screen != "home":
            d.rounded_rectangle((W / 2 - 70, H - 16, W / 2 + 70, H - 10), 3, fill=ink)  # home bar
        x, y = self.pointer  # AssistiveTouch pointer: a grey disc
        d.ellipse((x * W - POINTER_R, y * H - POINTER_R, x * W + POINTER_R, y * H + POINTER_R),
                  fill=(90, 90, 90) if self.pressed_at else (128, 128, 128), outline=(255, 255, 255), width=2)
        return img


class Encoder:
    """H.264 like iOS sends it to the board: no B-frames, SPS/PPS as a separate CONFIG unit before each IDR."""

    def __init__(self, gop: int = 10):
        self.n = 0
        self.ctx = av.CodecContext.create("libx264", "w")
        self.ctx.width, self.ctx.height, self.ctx.pix_fmt = W, H, "yuv420p"
        self.ctx.time_base = Fraction(1, 30)
        self.ctx.gop_size = gop
        self.ctx.options = {"preset": "ultrafast", "tune": "zerolatency", "crf": "30"}

    def encode(self, img: Image.Image) -> list[tuple[bytes, int]]:
        """(Annex-B data, flags) units for one frame: [CONFIG, IDR] or [P]."""
        frame = av.VideoFrame.from_image(img).reformat(format="yuv420p")
        frame.pts = self.n
        self.n += 1
        units = []
        for packet in self.ctx.encode(frame):
            nals = _nals(bytes(packet))
            config = b"".join(n for n in nals if n[4] & 0x1F in (7, 8))
            rest = b"".join(n for n in nals if n[4] & 0x1F not in (7, 8))
            idr = any(n[4] & 0x1F == 5 for n in nals)
            if config:
                units.append((config, ble.CONFIG))
            units.append((rest, ble.IDR if idr else 0))
        return units


def _nals(data: bytes) -> list[bytes]:
    """Split Annex-B into NAL units, each with a 4-byte start code."""
    starts, i = [], 0
    while (i := data.find(b"\x00\x00\x01", i)) >= 0:
        starts.append(i + 3)
        i += 3
    out = []
    for k, s in enumerate(starts):
        end = starts[k + 1] - 3 if k + 1 < len(starts) else len(data)
        while end > s and data[end - 1] == 0:  # the next 4-byte start code's leading zero
            end -= 1
        out.append(b"\x00\x00\x00\x01" + data[s:end])
    return out


class SimClient:
    """Stands in for bleak's BleakClient: the board's side of the GATT service, in process."""

    mtu_size = 247

    def __init__(self, board: "SimBoard"):
        self.board, self.phone = board, board.phone
        self.is_connected = True
        self.started = time.time()
        self.encoder = Encoder()
        self.gop: list[tuple[bytes, int, int]] = []  # config + AUs since the last IDR: (data, flags, ms)
        self.streaming = False
        self.seq = 0
        self.dirty = True
        self.out: asyncio.Queue[bytes] = asyncio.Queue()
        self.sent = 0

    def ms(self) -> int:
        return int((time.time() - self.started) * 1000)

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool = True) -> None:
        await asyncio.sleep(0.015)  # one connection event
        rid, _, line = bytes(data).decode().partition(" ")
        asyncio.get_running_loop().create_task(self._handle(rid, line))

    async def read_gatt_char(self, uuid: str) -> bytes:
        if uuid == ble.NET:
            return json.dumps({"mode": "ap", "ssid": "ESP32Agent-SIM0", "pass": "simulated123", "ip": "192.168.4.1"}
                              ).encode()
        return json.dumps(self.status()).encode()

    async def disconnect(self) -> None:
        self.is_connected = False

    def _reply(self, rid: str, text: str) -> None:
        self.board._on_reply(None, bytearray(f"{rid} {text}".encode()))

    async def _handle(self, rid: str, line: str) -> None:
        name, _, arg = line.partition(" ")
        p = self.phone
        try:
            nums = [int(n) for n in arg.split()] if name in ("move", "scroll") else []
        except ValueError:
            return self._reply(rid, "err bad numbers")
        match name:
            case "ping": pass
            case "home":
                p.move(-127 * 50, -127 * 50)
                await asyncio.sleep(0.1)
            case "move":
                p.move(*nums[:2])
                await asyncio.sleep(max(map(abs, nums[:2])) / 10 * 0.008)  # the firmware paces moves
            case "press": p.press()
            case "release": p.release()
            case "click":
                p.press()
                await asyncio.sleep(0.06)
                p.release()
            case "scroll" | "res" | "fps" | "coex": pass
            case "type":
                p.type(arg)
                await asyncio.sleep(0.02 * len(arg))
            case "key": p.key(arg)
            case "homescreen": p.go("home")
            case "stream":
                if arg not in ("on", "off"):
                    return self._reply(rid, "err usage: stream on|off")
                self.streaming = arg == "on"
            case "snap":
                if not self.gop:
                    return self._reply(rid, "err no video (start screen mirroring)")
                for data, flags, ms in list(self.gop):
                    self._send_au(data, flags | ble.SNAP, ms)
                await self.out.join()  # replies when the GOP is sent
            case _: return self._reply(rid, "err unknown command")
        self.dirty |= name not in ("ping", "stream", "snap", "res", "fps", "coex")
        self._reply(rid, "ok")

    def _send_au(self, data: bytes, flags: int, ms: int) -> None:
        room = self.mtu_size - 3 - 3
        payload = struct.pack("<I", ms) + data
        for i in range(0, len(payload), room):
            f = flags | (ble.START if i == 0 else 0) | (ble.END if i + room >= len(payload) else 0)
            self.out.put_nowait(struct.pack("<HB", self.seq & 0xFFFF, f) + payload[i:i + room])
            self.seq += 1

    def status(self) -> dict:
        return {"up": self.ms(), "rst": "power-on", "usb": "ok", "net": "ap", "ip": "192.168.4.1", "sta": 1,
                "ap": "streaming", "w": W, "h": H, "kbps": 0, "fps": 5.0, "idr": 2000,
                "gop": sum(len(d) for d, _, _ in self.gop),
                "ble": {"mtu": self.mtu_size, "phy": 2, "kbps": round(self.sent * 8 / 1000 / max(1, self.ms() / 1000)),
                        "drop": 0, "rdrop": 0, "n": 1}}

    async def run(self, kbps: float, fps: float = 5) -> None:
        """The board's tasks: the frame pump (AirPlay sends a frame only when the screen changed), the BLE
        sender at `kbps`, and the status heartbeat."""
        async def frames():
            while self.is_connected:
                if self.dirty:
                    self.dirty = False
                    ms = self.ms()
                    for data, flags in self.encoder.encode(self.phone.render()):
                        if flags & ble.CONFIG:
                            self.gop = []
                        self.gop.append((data, flags, ms))
                        if self.streaming:
                            self._send_au(data, flags, ms)
                await asyncio.sleep(1 / fps)

        async def sender():
            while self.is_connected:
                chunk = await self.out.get()
                await asyncio.sleep(len(chunk) * 8 / 1000 / kbps)
                self.sent += len(chunk)
                self.board._on_video(None, bytearray(chunk))
                self.out.task_done()

        async def heartbeat():
            while self.is_connected:
                await asyncio.sleep(1)
                self.board._on_status(None, bytearray(json.dumps(self.status()).encode()))

        await asyncio.gather(frames(), sender(), heartbeat())


class SimBoard(BleBoard):
    """A BleBoard connected to SimClient instead of a real board over CoreBluetooth."""

    def __init__(self, kbps: float = 200):
        super().__init__()
        self.kbps = kbps
        self.phone = SimPhone()

    async def _session(self) -> None:
        self.state, self.found = "connecting", "ESP32 Agent SIM0 (simulated)"
        client = SimClient(self)
        try:
            self._client = client
            self._ready = True
            self.links += 1
            self._last = self._beat = time.time()
            self.state = "connected"
            if self.on_link:
                await self._safely(self.on_link(True))
            self.net = json.loads(await client.read_gatt_char(ble.NET))
            await client.run(self.kbps)
        finally:
            client.is_connected = False
            self._ready, self._client = False, None
