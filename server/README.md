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
For OpenAI, `LLM_ORGANIZATION` optionally sets the `OpenAI-Organization` request header. Per-role
`ACTOR_ORGANIZATION` / `PLANNER_ORGANIZATION` values override it. Credentials belong in ignored `server/.env`.

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

## Capture from the main control page (manual playback, no model needed)

Run `server/relay.sh` from Terminal and open <http://127.0.0.1:8890>. With `SCREEN=uxplay`,
the **音视频采集 / Media capture** panel shares the same **iPhone Agent** AirPlay receiver as the live
screen. It does not require port 8891, a second mirroring connection, a model API key, or FrontierOS.

1. Mirror the phone to **iPhone Agent** and open the intended chat. Do not start playing yet.
2. Click **开始采集** in the capture panel (not the AI task's **开始** button).
3. Manually play the voice message or video on the phone, including the complete beginning and end.
4. Click **停止并保存**, then replay/download the MP4 and WAV in the same panel. The existing mirroring
   connection stays active throughout. The separate **紧急停止** also ends recording as interrupted.

The receiver always drains decoded audio, but persists media only during an explicit capture. Mac speakers
do not play this stream live. Captures are limited to 180 seconds and stored in ignored
`server/out/recordings/<id>/`. Version 2 transports each decoded audio/video buffer with its presentation
timestamp. UxPlay's pipeline base time and the buffer's running time place both streams on a shared clock;
bursty pipe delivery does not trim samples or manufacture pauses. Stop drains in-flight data for 250 ms.
Missing timestamps, transport errors and buffer overflow stop capture explicitly instead of silently losing media.

The small `handysink` GStreamer plugin builds automatically on relay startup into ignored `server/out/gstreamer/`.
It requires a C compiler, `pkg-config`, and GStreamer development headers (macOS: Xcode Command Line Tools,
`brew install gstreamer pkgconf`). UxPlay itself is unchanged. Restart the relay and reconnect mirroring after
updating; the panel then shows **已启用时间戳同步**. The sink disables playback pacing/QoS dropping while retaining
source PTS. Clock mapping follows GStreamer's [synchronization model](https://gstreamer.freedesktop.org/documentation/additional/design/synchronisation.html).

Audio retains the received sample rate and channel count as 16-bit PCM; no forced 16 kHz/mono conversion.
`capture.mp4` stores lossless ALAC audio, `audio-1.wav` exports that PCM without AAC loss, and `playback-1.mp4`
encodes AAC once at 256 kbps for browser compatibility. AirPlay/IM may already have resampled or compressed
the source, so this preserves the **received** quality, not the original attachment's bytes. Video is encoded
from decoded JPEG frames at their source presentation times; static frames are held until the next change.
Rotation is letterboxed into the initial dimensions. `timing.jsonl` and `result.json` retain format, timestamps,
sample counts and interruption diagnostics. Disconnects save a partial recording marked interrupted.
Real-device playback must still verify first/last words and lip sync; track presence alone is insufficient.

When models are configured, the Mac agent also supports `record_start`, `record_wait SECONDS` (1–180),
and `record_stop`. A capture task should locate the intended message, start recording before opening or
playing it, wait its duration, then save. This does not transcribe or understand the audio; results remain
local for verification. AI tasks require configured planner/actor models; manual playback does not.

## Standalone diagnostic receiver (optional)

Run `bash server/capture.sh` from the repository root, then open <http://127.0.0.1:8891>.
This optional low-level diagnostic uses UxPlay, ffmpeg and ffprobe. It does not load `.env`, connect to BLE,
operate the phone, call models, or integrate with FrontierOS. It is independent of the main control page;
do not use it simultaneously with the main page for a phone-operation demo.

1. Click **开始采集**. On the iPhone, select **iPhone Media Test** in Screen Mirroring, on the same network as the Mac.
2. Manually play one voice message or a short video with sound. The receiver consumes audio without playing it
   through Mac speakers; it records both streams using UxPlay's `-mp4` support (UxPlay 1.73+).
3. Click **停止并保存**. Graceful receiver shutdown finalizes the MP4. Each run is limited to 180 seconds,
   including time waiting for the phone to connect. Reconnect Screen Mirroring for every new run.
4. Replay the video and separate WAV on the page. Check the first/last words, expected duration, sound content,
   and audio/video synchronization. Track presence and signal energy alone do not prove a successful capture.

Artifacts are stored in ignored `server/out/media-captures/<id>/`: the received MP4, a browser playback copy
(video packets preserved, audio converted to AAC), WAV at the received rate/channel count, `result.json`, and `uxplay.log`.
These are captures of playback, not original IM attachments. A reconnect may produce multiple files, shown
separately; no message association or automatic clipping is performed. Empty, unreadable and silent captures
remain visible as diagnostics. The receiver uses ports 7400–7402 and a separate AirPlay device ID, so the
normal relay may stay running; its picture stops updating while the phone mirrors to the test receiver.
After testing, select **iPhone Agent** again to use the normal relay.

Automated checks use generated media and cannot establish that a particular iOS/IM version sends its audio
over AirPlay. Real-device voice and video playback must be checked separately. Avoid notifications during a
sample so that unrelated phone sounds do not contaminate it. Files stay on the Mac until manually removed.

## Legacy (`LINK=tcp`)

The computer-side server of the original version (tag `v0.1.0`, branch `legacy`): UxPlay on a Mac or Linux machine
receives the screen and the ESP32 is only a USB mouse that connects to it over Wi-Fi. It needs the legacy firmware
(check out `legacy` or `v0.1.0` and follow its README); the current firmware speaks BLE.
