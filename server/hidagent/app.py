"""Web API + control page. Run with `python -m hidagent`.

LINK=ble (default): the current firmware; this Mac talks to the board over Bluetooth.
  SCREEN=board (default): the board forwards the mirrored screen over BLE (only ~100 kbps to a Mac: laggy).
  SCREEN=uxplay: the phone mirrors to UxPlay on this Mac over Wi-Fi; the board only clicks and types.
LINK=tcp: the legacy v0.1.0 firmware over Wi-Fi, with UxPlay on this computer receiving the screen.
LINK=sim: a simulated board and phone (simboard.py), to try the relay without hardware.
"""

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agent import Agent
from .debug import Trace
from .device import Device, DeviceError
from .llm import LLM
from .phone import Phone

DATA_DIR = Path(os.environ.get("HIDAGENT_DATA", Path.home() / ".hidagent"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DEBUG = os.environ.get("HIDAGENT_DEBUG") == "1"
LINK = os.environ.get("LINK", "ble").lower()
SCREEN = "uxplay" if LINK == "tcp" else os.environ.get("SCREEN", "board").lower()

if LINK == "tcp":
    device = Device()
elif LINK == "sim":
    from .simboard import SimBoard

    device = SimBoard(float(os.environ.get("SIM_KBPS", 200)))
else:
    from .ble import BleBoard

    device = BleBoard(os.environ.get("BLE_NAME", ""), os.environ.get("BLE_ADDRESS", ""))
if SCREEN == "uxplay":
    from .screen import Screen

    port = os.environ.get("AIRPLAY_PORT") or ("" if LINK == "tcp" else "7200")  # 7000 may be macOS's own receiver
    screen = Screen(os.environ.get("AIRPLAY_NAME", "iPhone Agent"), port=int(port) if port else None)
else:
    from .blescreen import BleScreen

    screen = BleScreen(device, os.environ.get("FRAME_MODE", "stream"))
phone = Phone(device, DATA_DIR / ("calibration-sim.json" if LINK == "sim" else "calibration.json"), screen)
agent = Agent(phone, screen, LLM.from_env("ACTOR"), LLM.from_env("PLANNER"),
              Trace(DATA_DIR / "traces" if DEBUG else None))


@asynccontextmanager
async def lifespan(_):
    if LINK == "tcp":
        server = await device.serve("0.0.0.0", int(os.environ.get("DEVICE_PORT", 7300)))
    else:
        device.start()
    if SCREEN == "uxplay":
        screen.start()
    awake = asyncio.create_task(device.keep_awake(screen=screen))
    yield
    awake.cancel()
    agent.stop()
    if SCREEN == "uxplay":
        screen.stop()
    if LINK == "tcp":
        server.close()
    else:
        await device.stop()


app = FastAPI(lifespan=lifespan)
class Static(StaticFiles):
    """Static files that browsers re-check on every load, so a new version shows up on refresh."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", Static(directory=Path(__file__).parent / "static"), name="static")


class Point(BaseModel):
    x: float
    y: float


class Swipe(BaseModel):
    x1: float
    y1: float
    x2: float
    y2: float


class Scroll(Point):
    lines: int


class Text(BaseModel):
    text: str


async def act(coro):
    try:
        await coro
    except (DeviceError, RuntimeError) as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/status")
def status():
    return {"device": device.connected, "screen": screen.live, "calibrated": phone.calibrated,
            "debug": DEBUG, "trace": str(agent.trace.dir or ""), "aspect": round(screen.aspect, 4),
            "link": LINK, "screen_source": SCREEN, "airplay_name": getattr(screen, "name", "ESP32 Agent"),
            "board": board_info(),
            "models": {"planner": agent.planner.model, "actor": agent.actor.model},
            "agent": {"running": agent.running, "task": agent.task, "steps": agent.steps}}


def board_info() -> dict | None:
    """BLE only: link state, the board's heartbeat, and the video numbers (no passwords)."""
    if LINK == "tcp":
        return None
    st = device.status if device.connected else {}
    net = device.net or {}
    info = {"state": device.state, "name": device.found, "usb": st.get("usb"), "net": st.get("net") or net.get("mode"),
            "ssid": net.get("ssid"), "stations": st.get("sta"), "rst": st.get("rst"),
            "up_s": round(st.get("up", 0) / 1000) if st else None, "board_ble": st.get("ble")}
    if SCREEN == "uxplay":  # the screen comes from UxPlay on this Mac
        f = screen.frame
        return {**info, "airplay": "streaming" if screen.live else "idle", "frame_mode": "uxplay", "stream": True,
                "size": list(_jpeg_size(f)) if f else [0, 0],
                "frame_age": round(time.time() - screen.updated, 1) if screen.updated else None}
    return {**info, "airplay": st.get("ap"), "res": [st.get("w"), st.get("h")], "fps": st.get("fps"),
            "kbps_in": st.get("kbps"), "ble_kbps": round(screen.kbps), "frame_mode": screen.frame_mode,
            "stream": screen.stream_on, "size": list(screen.size),
            # since the board got the shown frame (a stalled stream shows here)
            "frame_age": round(time.time() - (screen.captured_at or screen.updated), 1) if screen.updated else None,
            "note": screen.note,
            # video path diagnostics: the board's side (AirPlay in, BLE out) and ours (chunks lost, frames decoded)
            "idr_ms": st.get("idr"), "gop_bytes": st.get("gop"),
            "lost_chunks": screen.lost, "decoded": screen.decoded, "broken": screen.broken}


def _jpeg_size(jpeg: bytes) -> tuple[int, int]:
    """Width, height from a baseline JPEG header."""
    i = jpeg.find(b"\xff\xc0")
    return (int.from_bytes(jpeg[i + 7:i + 9], "big"), int.from_bytes(jpeg[i + 5:i + 7], "big")) if i >= 0 else (0, 0)


@app.get("/api/board/hotspot")
def hotspot():
    """The board's hotspot SSID and password (BLE only), for joining it from the phone. Local page only."""
    if LINK == "tcp" or not device.net:
        raise HTTPException(404, "not read yet: accept the Bluetooth pairing request on this Mac")
    return {k: device.net.get(k) for k in ("mode", "ssid", "pass", "ip")}


@app.post("/api/snap")
async def snap():
    """Fetch the board's current screen now (BLE only; the live stream normally keeps it fresh)."""
    if SCREEN == "uxplay":
        raise HTTPException(404)
    try:
        return {"ok": True, "ms": round(await screen.snap())}
    except DeviceError as e:
        raise HTTPException(409, str(e)) from e


@app.get("/api/frame.jpg")
def frame():
    if not screen.live:
        raise HTTPException(404, "no live screen: start screen mirroring on the phone")
    return Response(screen.frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/steps/{index}.jpg")
def step_shot(index: int):
    if index not in agent.shots:
        raise HTTPException(404)
    return Response(agent.shots[index], media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


@app.post("/api/tap")
async def tap(p: Point):
    return await act(phone.tap(p.x, p.y))


@app.post("/api/swipe")
async def swipe(s: Swipe):
    return await act(phone.swipe(s.x1, s.y1, s.x2, s.y2))


@app.post("/api/scroll")
async def scroll(s: Scroll):
    return await act(phone.scroll(s.x, s.y, s.lines))


@app.post("/api/type")
async def type_text(t: Text):
    return await act(phone.type(t.text))


@app.post("/api/key")
async def key(t: Text):
    return await act(phone.key(t.text))


@app.post("/api/home")
async def home():
    return await act(phone.go_home())


@app.post("/api/back")
async def back():
    return await act(phone.go_back())


@app.post("/api/raw")
async def raw(t: Text):
    """Debug only: send one protocol line to the board as-is."""
    if not DEBUG:
        raise HTTPException(404)
    return await act(device.send(t.text))


@app.post("/api/calibrate/start")
async def calibrate_start():
    """Calibrate the pointer. {"auto": true}: done by itself; otherwise the user clicks the pointer next."""
    try:
        return {"ok": True, "auto": await phone.start_calibration(), "span": phone.span}
    except (DeviceError, RuntimeError) as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/calibrate/finish")
def calibrate_finish(p: Point):
    phone.finish_calibration(p.x, p.y)
    return {"ok": True, "span": phone.span}


@app.post("/api/agent")
async def agent_start(t: Text):
    try:
        agent.start(t.text)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


@app.post("/api/agent/stop")
async def agent_stop():
    """Emergency stop: cancel the agent and release the mouse button."""
    agent.stop()
    await asyncio.sleep(0.1)  # let a gesture in flight finish its own release
    return await act(phone.reset()) if device.connected else {"ok": True}


@app.post("/api/agent/clear")
def agent_clear():
    """Forget the last run (steps, screenshots, plan) so the page starts clean."""
    try:
        agent.clear()
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}
