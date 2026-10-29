# Mac 中继：用 Mac mini 上的 AI 操作 iPhone

原项目当前版本把 AI 放在 iPhone 上的 App 里（需要 Xcode 编译、签名，免费账号每 7 天重签）。
这个分支（`mac-ble-relay`）让 **Mac mini 取代那个 App**：Mac 通过蓝牙连接开发板，和 App 走的是完全相同的协议
（[ble-protocol.md](ble-protocol.md)）。固件不用改，手机上也不用装任何东西。

```
iPhone ──屏幕镜像(家里的 Wi-Fi)──▶ Mac mini：UxPlay「iPhone Agent」→ 视觉模型 → 动作
   ▲                                                                        │
   └── USB HID 鼠标 + 键盘 ◀── ESP32-S3（插在手机上）◀──── BLE 命令 ◀──────────┘
```

- **眼睛**：手机通过家里的 Wi-Fi 把屏幕镜像到 Mac 上的 UxPlay（接收器名字 **iPhone Agent**），画面实时、清晰（`SCREEN=uxplay`）。
- **手**：开发板插在手机上，是一个 USB 鼠标 + 键盘；打开「辅助触控」后鼠标指针就能点按、滑动。命令走蓝牙，数据量很小。
- **大脑**：Mac 上的 Python 中继（`server/`）把截图发给任意 OpenAI 兼容的视觉模型，规划器出计划、执行器出动作，
  再把鼠标、键盘命令发回开发板。浏览器打开 `http://127.0.0.1:8890` 就能看实时画面、下任务、急停、回看每一步。

## 实施路线

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 代码放在 `~/code/esp32-iphone-agent`（路径不能有空格，刷机脚本要求），分支 `mac-ble-relay`；`server/.venv` 是 Python 3.12 环境 | ✅ 已完成 |
| 1 | 中继软件：蓝牙链路、视频解码、指针校准、Agent 循环、网页控制台；模拟开发板 + 测试 | ✅ 已完成（34 个测试通过，模拟模式下跑通了整个任务） |
| 2 | 安装 arduino-cli 并刷固件 | ✅ 已完成：板子是 ESP32-S3（8 MB PSRAM + 8 MB Flash），固件已刷入并校验；Mac 能通过蓝牙连上「ESP32 Agent D10D」 |
| 3 | 视觉模型 | ✅ OpenAI `gpt-6-astra`，真实调用已测通（见第 5 步） |
| 4 | 画面通道 | ✅ 改为 Mac 接收 AirPlay（UxPlay）：开发板转发的蓝牙视频到 Mac 只有约 60–110 kbps，手机一动就积压一分多钟；改走 Wi-Fi 后画面实时 |
| 5 | 真机：指针校准、第一个任务 | ⏳ 进行中 |

## 硬件要求

- **ESP32-S3**，USB-C 口必须是芯片原生 USB，并且带 PSRAM。作者测试用的是 Seeed Studio XIAO ESP32S3（普通版，8 MB PSRAM）。
  `hardware/flash.sh` 按 8 MB Flash + OPI（八线）PSRAM 编译：`N8R8`、`N16R8` 这类模组可以直接用；`N8R2`（四线 PSRAM）
  需要把脚本里的 `PSRAM=opi` 改成 `PSRAM=enabled`。**普通 ESP32、ESP32-C3 不行**（没有原生 USB 或 PSRAM 不够）。
- **iPhone**：iOS 18 及以上。USB-C 机型直接插；Lightning 机型需要「Lightning 转 USB 相机转换器」。手机给开发板供电。
- **Mac mini**：自带蓝牙即可。开发板插在手机上，Mac 和手机放在几米之内。

## 1. 安装刷机工具（只需一次，这台 Mac mini 上已经装好）

已安装：`~/.local/bin/arduino-cli`（1.5.1 正式版，来自 GitHub Releases 的官方 macOS ARM64 包，Arduino 签名）、
ESP32 core 3.3.12、NimBLE-Arduino 2.5.1，以及 Rosetta 2（Arduino 的 ctags 工具只有 Intel 版，Apple 芯片的 Mac 编译时要靠它，
否则会报 `bad CPU type in executable`；安装命令：`softwareupdate --install-rosetta --agree-to-license`）。`~/.local/bin` 默认不在 PATH 里，用之前先执行（也可以写进 `~/.zshrc`）：

```bash
export PATH="$HOME/.local/bin:$HOME/Library/Python/3.9/bin:$PATH"   # arduino-cli 和 uv
```

在别的 Mac 上重新安装：

```bash
mkdir -p ~/.local/bin && cd /tmp
curl -fLO https://github.com/arduino/arduino-cli/releases/download/v1.5.1/arduino-cli_1.5.1_macOS_ARM64.tar.gz
tar -xzf arduino-cli_1.5.1_macOS_ARM64.tar.gz arduino-cli && mv arduino-cli ~/.local/bin/
export PATH="$HOME/.local/bin:$PATH"
arduino-cli core update-index --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core install esp32:esp32@3.3.12 --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli lib install "NimBLE-Arduino@2.5.1"
```

（版本与上游 CI 一致：ESP32 core 3.3.12，NimBLE-Arduino 2.5.1。ESP32 工具链约 1 GB。）

## 2. 刷固件

用数据线把开发板插到 **Mac mini**，然后：

```bash
cd ~/code/esp32-iphone-agent
export PATH="$HOME/.local/bin:$PATH"
hardware/flash.sh
```

- 必须用 `flash.sh` 编译：它会注入 `hid_bridge/nimble_cfg.h`（BLE 缓冲区大小），直接用 IDE 编译会失败。
- 全新的板子如果没出现 `/dev/cu.usbmodem*`：按住 BOOT 键再插线，进入下载模式后重试。
- 刷完把板子拔下，插到 **iPhone** 上。

## 2b. 安装 UxPlay（Mac 接收屏幕镜像，只需一次，这台 Mac mini 上已经装好）

UxPlay 不在 Homebrew 里，按它 README 的方法从源码编译（这台 Mac 上是 v1.73.7，装在 `~/.local/bin/uxplay`）：

```bash
brew install cmake pkg-config gstreamer libplist openssl@3
curl -fLO https://github.com/FDH2/UxPlay/archive/refs/tags/v1.73.7.tar.gz && tar -xzf v1.73.7.tar.gz
cd UxPlay-1.73.7 && mkdir build && cd build && cmake .. && make -j8 && cp uxplay ~/.local/bin/
```

中继会自己启动 UxPlay：名字 `iPhone Agent`，端口 7200–7202（macOS 自带的隔空播放接收器占着 7000，所以不用默认端口），
`-vsync no`（收到就显示，不等时间戳）。

## 3. 手机设置（只需一次）

1. 设置 → 辅助功能 → 触控 → **辅助触控** → 打开。插上开发板后屏幕上会出现圆形指针。
2. 设置 → 辅助功能 → **指针控制** → 关闭「**指针动画**」和「**自动隐藏指针**」。开着指针动画时，指针停在图标上会变成图标高亮，
   校准时找不到圆点。
3. 辅助触控 → **跟踪速度**拖到中间。太快的话，校准的第二个点会超出屏幕。

## 4. 启动中继（必须在「终端」App 里运行）

```bash
cd ~/code/esp32-iphone-agent
server/relay.sh
```

- 第一次运行会生成 `server/.env`：在里面填 `LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY`（见第 5 步），保存后重新运行。
- macOS 会弹出两次询问：「终端」想使用蓝牙 → **允许**；配对请求「ESP32 Agent XXXX」→ **连接/配对**。
- **一定要在 macOS 自带的「终端」（或 iTerm）里运行。** 从没有蓝牙权限的程序里启动时，macOS 会直接终止 Python 进程（崩溃报告里能看到
  `NSBluetoothAlwaysUsageDescription`）。权限在 系统设置 → 隐私与安全性 → 蓝牙 里管理。

浏览器打开 <http://127.0.0.1:8890>：

1. 右侧「开发板」一行显示 `connected`。
2. iPhone 连**家里的 Wi-Fi**（和 Mac mini 同一个网络；开发板的热点用不上）。
3. iPhone：控制中心 → 屏幕镜像 → **iPhone Agent**。**不要选以 Mac 主机名显示的接收器**：那是 macOS 自带的接收器，会把手机画面
   全屏铺满 Mac 显示器，中继收不到。选对后 Mac 屏幕上不会出现手机画面，网页左侧出现实时画面。
4. 手动操控 → **校准**：中继会自己晃动指针找到它；找不到时按提示在画面里点一下圆形指针。
5. 输入任务（例如「打开设置，进入 Wi-Fi」），点 **开始**。随时可以按 Esc 或「紧急停止」。

画面里点一下 = 点按，拖动 = 滑动，滚轮 = 滚动，也可以直接手动操作手机。

## 5. 视觉模型

任何 OpenAI 兼容、支持图片输入的接口都可以：云 API（OpenAI、阿里云百炼 Qwen-VL、OpenRouter 等），
或者本机/局域网里用 vLLM、Ollama、llama.cpp 部署的模型。`server/.env` 示例：

```bash
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=YOUR_VISION_MODEL_ID  # 替换为服务商模型列表中的视觉模型 ID
LLM_API_KEY=sk-...
# 可选：执行器要快，规划器可以更强
# ACTOR_MODEL=...
# PLANNER_MODEL=...
```

提示词在仓库的 `prompts/`（和 iOS App 用的是同一份），改完重启中继即可生效。

**这台 Mac mini 当前的配置**（`server/.env`，已被 git 忽略，权限 600）：OpenAI 的 `gpt-6-astra`（2026 年 10 月 OpenAI 文档里能力最强的模型），
`LLM_EXTRA_BODY='{"reasoning_effort": "low"}'`（Astra 最快的一档），`LLM_TOKEN_PARAM=max_completion_tokens`。
实测一次规划约 3.3 秒，一次动作决策约 2.6 秒，在模拟主屏上点「备忘录」图标只偏了约千分之一屏幕。
Astra 价格是每百万 token 输入 $10 / 输出 $50；想要更快、更便宜，可以把 `ACTOR_MODEL` 换成 `gpt-6.1-terra`
（OpenAI 介绍它擅长视觉和电脑操作，价格是 Astra 的十分之一），规划器继续用 Astra。
这类推理模型只接受默认 temperature，中继第一次被拒绝后会自动不再发送这个参数。

## 没有硬件时先试一下

```bash
SCREEN=board server/relay.sh --sim
```

模拟模式下有一块「虚拟开发板」：它画出一部假的 iPhone（主屏幕、设置、备忘录、时钟），
像固件一样把 H.264 视频切成蓝牙分片、按 Mac 的蓝牙速率发送。网页、校准、点按、输入，以及 AI 任务都能完整跑一遍，
走的就是真机用的那套蓝牙/解码代码。

## 排错

| 现象 | 处理 |
|---|---|
| Python 一启动就崩溃（`NSBluetoothAlwaysUsageDescription`） | 改用「终端」App 运行，并在 系统设置 → 隐私与安全性 → 蓝牙 里允许它 |
| 一直显示 scanning / not found | 板子要有电（插在手机上），并且刷的是当前固件。附近有多块板子时用 `BLE_NAME=ESP32 Agent 1A2B` 指定 |
| 没有画面 | 手机和 Mac 要在同一个 Wi-Fi，屏幕镜像选 **iPhone Agent**。锁屏后镜像会断开；任务进行中断线时会暂停最多 90 秒等待恢复 |
| Mac 屏幕被手机画面占满 | 选成了 macOS 自带的接收器。手机上「停止镜像」后改选 iPhone Agent；也可以在 系统设置 → 通用 → 隔空投送与接力 关掉「隔空播放接收器」 |
| 画面不动 | 中继终端里看 UxPlay 的输出；`server/.env` 里确认 `SCREEN=uxplay`。`SCREEN=board`（开发板经蓝牙转发画面）在 Mac 上会严重滞后，只适合测试 |
| 点偏了 | 重新校准；执行器会对小目标用 `tap ... precise`（先找到指针再修正）；点偏的经验也会被记住 |
| 想看每一步发给模型的内容 | `.env` 里设 `HIDAGENT_DEBUG=1`，记录在 `~/.hidagent/traces/` |

## 改了哪些代码

- `server/hidagent/ble.py`（新）：蓝牙链路。负责扫描、连接、断线重连，按请求 ID 匹配 `<id> <命令>` 的回复；
  超时后先关掉视频流再重试一次；视频分片重组；读取 `net`（热点信息，首次读取会触发配对）。
- `server/hidagent/blescreen.py`（新）：视频画面，移植自 App 的 VideoFeed。在后台线程里解码 H.264，按开发板时钟换算出每一帧的采集时间。
  平时用 `stream on` 实时流，拿不到最新帧时退回 `snap`。`after_action()` 保证模型不会看到动作之前的旧画面。
- `server/hidagent/simboard.py`（新）：模拟开发板 + 假手机（`LINK=sim`）。
- `server/hidagent/vision.py`：找指针时忽略 H.264 压缩噪点，只取最密集的那一团变化像素（低码率的蓝牙画面原来会找不到指针）。
- `server/hidagent/agent.py`：提示词改从 `prompts/` 读取；每一轮开始前准备好画面，动作之后等待新画面；
  断线时暂停等待，不会直接失败；蓝牙超时也计入失败次数。
- `server/hidagent/phone.py`：回主屏幕优先用固件的 Home 键（`homescreen`）。
- `server/hidagent/screen.py` + `app.py`：`SCREEN=uxplay`，开发板只管鼠标键盘，画面来自 Mac 上的 UxPlay。UxPlay 用自定义端口和
  `-vsync no`；管道里去掉了依赖时间戳的 `videorate`（否则只出第一帧）；在 macOS 上用 `lsof` 看 UxPlay 有没有手机连着，
  判断镜像是否还在，屏幕长时间不动也不会被当成断开。
- `server/hidagent/app.py`、`static/`：`LINK=ble|sim|tcp` 三种模式；网页显示开发板状态和热点信息；增加「刷新画面」按钮。网页默认只监听本机。
- 旧的 Wi-Fi + UxPlay 方式（`LINK=tcp`）仍然保留，原有测试全部通过。

原项目采用 MIT 许可证（`hardware/hid_bridge/src/airplay/` 是 GPL-3.0）。以后如果公开发布或演示这个修改版，
请保留 `LICENSE` 里的版权声明；原项目的 `NOTICE` 还要求注明出处。
