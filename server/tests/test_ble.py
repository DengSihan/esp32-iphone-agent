import asyncio
import struct
import time

import pytest

from hidagent import ble
from hidagent.agent import Agent
from hidagent.ble import BleBoard, Reassembler
from hidagent.blescreen import BleScreen
from hidagent.debug import Trace
from hidagent.device import BoardTimeout, DeviceError
from hidagent.phone import Phone
from hidagent.simboard import SimBoard


def chunk(seq: int, flags: int, payload: bytes) -> bytes:
    return struct.pack("<HB", seq, flags) + payload


def test_reassembler_rebuilds_units_and_waits_for_an_idr_after_a_gap():
    r = Reassembler()
    assert r.feed(chunk(0, ble.START | ble.IDR, struct.pack("<I", 1234) + b"ab")) is None
    unit = r.feed(chunk(1, ble.END, b"cd"))
    assert (unit.data, unit.board_ms, unit.idr) == (b"abcd", 1234, True)
    # seq 2 lost: the P-frame that follows cannot be decoded, the next IDR can
    assert r.feed(chunk(3, ble.START | ble.END, struct.pack("<I", 1) + b"p")) is None
    assert r.drops == 1
    unit = r.feed(chunk(4, ble.START | ble.END | ble.IDR, struct.pack("<I", 2) + b"i"))
    assert unit.data == b"i"
    assert r.feed(chunk(5, ble.BENCH, b"filler")) is None  # bench filler is ignored


class FakeClient:
    """A board that answers each command line by script."""

    is_connected = True

    def __init__(self, board: BleBoard, answer):
        self.board, self.answer, self.lines = board, answer, []

    async def write_gatt_char(self, uuid, data, response=True):
        rid, _, line = data.decode().partition(" ")
        self.lines.append(line)
        reply = self.answer(line)
        if reply is not None:
            asyncio.get_running_loop().call_soon(self.board._on_reply, None, bytearray(f"{rid} {reply}".encode()))


def connected(answer) -> tuple[BleBoard, FakeClient]:
    board = BleBoard()
    board._client = FakeClient(board, answer)
    board._ready = True
    return board, board._client


def test_send_matches_replies_by_id_and_raises_board_errors():
    async def scenario():
        board, client = connected(lambda line: "err unknown command" if line == "bad" else "ok")
        board._on_reply(None, bytearray(b"999 ok"))  # another central's reply: ignored
        assert await board.send("ping") == ""
        with pytest.raises(DeviceError, match="unknown command"):
            await board.send("bad")
        await board.send("type " + "x" * 400)  # split into board-sized writes
        assert [len(line) for line in client.lines[2:]] == [5 + 160, 5 + 160, 5 + 80]

    asyncio.run(scenario())


def test_a_timeout_runs_the_fallback_then_retries_once():
    async def scenario():
        answers = iter([None, "ok"])  # the first `click` gets no reply
        board, client = connected(lambda line: next(answers))
        fallbacks = []

        async def on_timeout(command):
            fallbacks.append(command)

        board.on_timeout = on_timeout
        await board.send("click", timeout=0.2)
        assert fallbacks == ["click"] and client.lines == ["click", "click"]
        board.on_timeout = None
        client.answer = lambda line: None
        with pytest.raises(BoardTimeout):
            await board.send("click", timeout=0.2)

    asyncio.run(scenario())


def test_send_without_board_fails_fast():
    with pytest.raises(DeviceError, match="not connected"):
        asyncio.run(BleBoard().send("ping"))


async def sim_ready(tmp_path, kbps=2000):
    board = SimBoard(kbps)
    screen = BleScreen(board)
    phone = Phone(board, tmp_path / "calibration.json", screen)
    board.start()
    for _ in range(100):
        if screen.live:
            break
        await asyncio.sleep(0.1)
    assert screen.live, board.state
    return board, screen, phone


def test_simulated_board_calibrates_and_taps_over_the_ble_path(tmp_path):
    """Mouse commands and H.264 frames go through the real BleBoard / BleScreen code."""

    async def scenario():
        board, screen, phone = await sim_ready(tmp_path)
        assert screen.size == (480, 1040) and screen.frame[:2] == b"\xff\xd8"
        assert await phone.start_calibration()  # found the pointer twice in the decoded frames
        assert abs(phone.span["w"] - 400) < 20 and abs(phone.span["h"] - 860) < 40
        before = screen.frame
        acted = time.time()
        await phone.tap(0.3825, 0.156, aim=True)  # the Notes icon
        await screen.after_action(acted)
        assert board.phone.screen == "Notes"
        assert screen.frame != before
        assert await screen.snap() > 0  # the board's GOP, decoded
        await phone.go_home()  # the firmware's Home key
        await asyncio.sleep(0.5)
        assert board.phone.screen == "home"
        await board.stop()

    asyncio.run(scenario())


class ScriptedLLM:
    def __init__(self, model, replies):
        self.model, self.replies, self.images = model, list(replies), []

    async def chat(self, system, text, image=None, **_):
        self.images.append(image)
        return self.replies.pop(0)


def test_agent_runs_a_task_on_the_simulated_phone(tmp_path):
    async def scenario():
        board, screen, phone = await sim_ready(tmp_path)
        assert await phone.start_calibration()
        planner = ScriptedLLM("planner", ["1. Open Notes\n2. Write hello"])
        actor = ScriptedLLM("actor", ["THINK: I see the home screen.\nDO: tap 383 156 precise",
                                      "THINK: Notes is open.\nDO: tap 500 300\nDO: type hello",
                                      "THINK: hello is written.\nDO: done wrote hello"])
        agent = Agent(phone, screen, actor, planner, Trace(None))
        agent.start("Open Notes and write hello")
        await asyncio.wait_for(agent._job, 60)
        assert [s["action"] for s in agent.steps] == ["plan", "tap", "tap", "type", "done"], agent.steps
        assert agent.steps[1]["changed"] is True
        assert board.phone.note == "hello"
        assert all(img and img[:2] == b"\xff\xd8" for img in actor.images)  # every decision saw a real frame
        await board.stop()

    asyncio.run(scenario())


def test_uxplay_screen_mode_reports_the_board_without_ble_video():
    """SCREEN=uxplay: mouse and keyboard over BLE, the screen from UxPlay (not started here)."""
    import json
    import os
    import subprocess
    import sys

    code = ("from hidagent import app; from hidagent.screen import Screen; "
            "assert isinstance(app.screen, Screen) and app.screen.port == 7200; "
            "import json; print(json.dumps(app.status()))")
    env = {**os.environ, "LINK": "sim", "SCREEN": "uxplay", "HIDAGENT_DATA": os.environ.get("TMPDIR", "/tmp")}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True).stdout
    status = json.loads(out.strip().splitlines()[-1])
    assert status["screen_source"] == "uxplay" and status["airplay_name"] == "iPhone Agent"
    assert status["board"]["frame_mode"] == "uxplay" and status["board"]["airplay"] == "idle"
