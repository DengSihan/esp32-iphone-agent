# server/: the Mac relay (BLE) and the legacy server

## Mac relay (current firmware, `LINK=ble`, the default)

A Mac takes the place of the iPhone app: it connects to the board over Bluetooth exactly as the app does
([docs/ble-protocol.md](../docs/ble-protocol.md)), decodes the mirrored H.264 screen the board forwards, runs the
planner/actor agent with any OpenAI-compatible vision model, and sends the mouse and keyboard commands back. The
firmware is unchanged and nothing is installed on the phone.

```
iPhone ──Screen Mirroring──▶ ESP32-S3 (AirPlay receiver, own hotspot) ──BLE──▶ Mac relay: decode → model → action
   ▲                                                                                   │
   └──────── USB HID mouse + keyboard ◀──────────── BLE commands ◀─────────────────────┘
```

```bash
server/relay.sh          # from Terminal.app (macOS asks Terminal for Bluetooth access); web UI on http://127.0.0.1:8890
SCREEN=board server/relay.sh --sim    # simulated board and phone; AI tasks still need a configured model
```

The first run copies `.env.example` to `.env`: set `LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY`. Prompts are read
from `prompts/` (the same ones the app sends). Step-by-step setup in Chinese: [docs/mac-relay.zh-CN.md](../docs/mac-relay.zh-CN.md).

For direct Wi-Fi video on a Mac, install UxPlay and GStreamer and set `LINK=ble`, `SCREEN=uxplay`. Keep the
phone and Mac on the same Wi-Fi and mirror to **iPhone Agent** (`AIRPLAY_NAME`). The board then only handles
HID commands over BLE. The default `SCREEN=board` forwards video over BLE and may lag significantly on a Mac.

| File | Role |
|---|---|
| `hidagent/ble.py` | BLE link: scan, connect, reconnect, `<id> <command>` / reply matching, video chunk reassembly |
| `hidagent/blescreen.py` | H.264 decode (PyAV), frame timing from the board clock, `stream on` with `snap` fallback |
| `hidagent/simboard.py` | `LINK=sim`: in-process board + fake phone that encodes H.264 and chunks it like the firmware |
| `hidagent/agent.py`, `phone.py`, `vision.py`, `app.py`, `static/` | agent loop, pointer, image checks, web UI (shared with the legacy mode) |

Tests: `uv run --extra dev pytest` (the simulated board runs calibration, taps and a whole agent task through the
BLE code path).

## Legacy (`LINK=tcp`)

The computer-side server of the original version (tag `v0.1.0`, branch `legacy`): UxPlay on a Mac or Linux machine
receives the screen and the ESP32 is only a USB mouse that connects to it over Wi-Fi. It needs the legacy firmware
(check out `legacy` or `v0.1.0` and follow its README); the current firmware speaks BLE.
