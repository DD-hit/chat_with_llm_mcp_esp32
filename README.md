# Chat with LLM & MCP 多工具服务（ESP32 智能语音交互）

## 项目简介

本项目是一个软硬件结合的智能语音助手解决方案，基于 ESP32-S3 开发板和 Python 服务端。用户通过语音与 ESP32 交互，音频实时传输到服务端，服务端自动完成语音识别（ASR）、大语言模型（LLM）推理、语音合成（TTS）等处理，并支持通过 MCP 工具服务扩展硬件控制（如开关灯、天气查询等）。硬件控制指令通过 MQTT 协议下发，ESP32 监听并执行。系统支持多轮对话、唤醒词检测、命令词识别、流式音频传输，具备良好的可扩展性和多工具集成能力，适合智能家居、语音控制等场景。

---

## 项目结构

```
chat_with_llm/
├── main/                # ESP32固件代码
│   └── main.cc
├── server/              # 服务端主程序及配置
│   ├── llm_mcp.py       # MCP多工具客户端，负责加载mcp_config.json，管理MCP工具服务与大模型交互
│   ├── server.py        # WebSocket主服务，负责与ESP32通信、音频收发、ASR/LLM/TTS处理
│   ├── mcp_config.json  # MCP工具服务配置文件，定义各工具服务的脚本路径、类型等参数
│   └── mcp-server/      # MCP工具服务脚本
├── tools/               # ASR/TTS等工具模块
├── models/              # ASR模型文件
├── prompt/              # LLM提示词
└── README.md
```

---

## 安装依赖

建议在 `server` 目录下执行：

```bash
cd server
pip install -r requirements.txt
```

---

## 硬件与参数配置

### ESP32硬件连接

- **麦克风（INMP441）**
  - VDD → 3.3V
  - GND → GND
  - SD → GPIO6
  - WS → GPIO4
  - SCK → GPIO5
  - 采样率：16000Hz，位宽：16bit，单声道

- **功放（MAX98357A）**
  - DIN → GPIO7
  - BCLK → GPIO15
  - LRC → GPIO16
  - VIN → 3.3V
  - GND → GND

- **LED**
  - 正极 → GPIO21
  - 负极 → GND

### 网络与协议

- **WiFi**  
  - SSID/PASS：请根据实际填写
- **MQTT**  
  - Broker地址：mqtt://Broker-IP:1883
  - 控制主题：esp32/led/control
  - 状态主题：esp32/led/state
- **WebSocket**  
  - 地址：ws://服务器ip:8888

### 音频参数

- **ESP32 → 服务端**
  - PCM字节流，采样率16000Hz，16bit，单声道
- **服务端 → ESP32**
  - PCM（WAV容器），采样率16000Hz，16bit，单声道

### 本地命令词（快速响应）

- ESP32 支持配置本地命令词（如“拜拜”、“现在安全屋情况如何”），无需联网即可快速响应。
- 命令词可在固件 `main.cc` 的 `custom_commands` 列表中自定义，支持多种本地语音控制场景。
- 本地命令词识别由 ESP-SR 框架完成，响应速度快，适合常用控制指令。

---

## 系统架构与流程

```
+-------------------+
|     ESP32         |
+---------+---------+
          | WebSocket
+---------v---------+
|     服务端        |
+---------+---------+
          | MQTT
+---------v---------+
|   MCP工具服务     |
+-------------------+
```

1. ESP32采集音频，通过WebSocket发送到服务端
2. 服务端ASR转文字，提交LLM处理
3. LLM自动决定是否调用MCP工具（如开关灯、查天气）
4. MCP工具通过MQTT控制硬件
5. 服务端TTS合成音频，返回ESP32播放

---

## 快速部署与运行

### 1. 配置 MCP 服务

编辑 `server/mcp_config.json`，添加所需 MCP 工具服务。例如：

```json
{
  "servers": {
    "led_controller": {
      "script_path": "mcp-server/led/mcp-led.py",
      "type": "python",
      "args": [],
      "env": {},
      "description": "LED控制服务器"
    },
    "weather": {
      "script_path": "mcp-server/weather/mcp-weather.py",
      "type": "python",
      "args": [],
      "env": {},
      "description": "天气数据读取服务器"
    }
  },
  "default_servers": ["led_controller", "weather"]
}
```

### 2. 启动服务端（请在 server 目录下）

```bash
cd server
python llm_mcp.py                    # 连接默认配置的服务器
python llm_mcp.py --all              # 显式连接所有配置的服务器
python llm_mcp.py -s led_controller  # 仅连接LED控制器
python llm_mcp.py -s led -s weather  # 连接LED和天气服务器
python llm_mcp.py -c mcp_config.json # 使用自定义配置文件
```

### 3. 启动 ESP32 固件

- 配置 WebSocket 地址与 MQTT 参数
- 配置麦克风和 LED 引脚（见上文）
- 上传并运行固件，确保能与服务端正常通信

---

## 固件编译与烧录教程

### 1. 安装 ESP-IDF 环境

请参考 [ESP-IDF 官方文档](https://docs.espressif.com/projects/esp-idf/zh_CN/latest/esp32/get-started/index.html) 完成环境安装。

### 2. 编译固件

在项目根目录下执行：

```bash
cd main
idf.py set-target esp32s3   # 设置目标芯片（如 ESP32-S3）
idf.py menuconfig           # 配置参数（唤醒词、Flash等）
idf.py build                # 编译固件
```

### 3. 烧录固件

连接开发板，执行：

```bash
idf.py -p <串口号> flash
```

例如：

```bash
idf.py -p COM3 flash
```

### 4. 查看串口日志（可选）

```bash
idf.py -p <串口号> monitor
```

### 5. 常见问题

- 若编译或烧录失败，请检查 USB 驱动、串口号、芯片型号设置是否正确。
- 如需自定义命令词或参数，请在 `main.cc` 或 `menuconfig` 中修改。

---

## 依赖环境

- Python 3.8+
- FunASR
- 智谱 AI SDK
- TTS（如 edge-tts 或其他）
- websockets, pydub, numpy, scipy, python-dotenv
- MQTT Broker（如 EMQX/Mosquitto）
- MCP 工具服务（需自行实现并配置）

---

## 扩展说明

- MCP 工具服务需实现标准 MCP 协议，支持 stdio 通信
- 灯光控制等硬件操作通过 MQTT 实现，需保证 ESP32 与 MCP 服务的 MQTT 配置一致
- 语音识别与合成可根据实际需求替换为其他方案
- 支持多工具服务并发扩展，配置文件灵活添加

---

## 备注

- 固件硬件参数（麦克风、LED 引脚等）请根据实际板型和需求调整
- MCP 工具服务需实现标准 MCP 协议，支持 stdio 通信
- 灯光控制等硬件操作通过 MQTT 实现，需保证 ESP32 与 MCP 服务的 MQTT 配置一致
