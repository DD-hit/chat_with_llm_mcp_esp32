#!/usr/bin/env python3
"""
🎯 ESP32智能语音助手WebSocket服务器

主要功能：
- 接收ESP32发送的实时音频流
- 语音识别（ASR）转文本
- 文本发送给大模型（LLM），支持工具调用
- 大模型回复转为语音（TTS），实时返回ESP32播放
- 支持多客户端并发、音频保存（可选）

环境要求：
- Python 3.8+
- websockets, pydub, numpy, scipy, funasr, edge-tts 等依赖
- ESP32与服务器同一局域网
"""

import os
import sys
import json
import wave
import asyncio
import websockets
from datetime import datetime
from pydub import AudioSegment
import socket

# === 主要配置区 ===
SAMPLE_RATE = 16000  # ESP32采样率
CHANNELS = 1         # 单声道
BIT_DEPTH = 16       # 16位深度
BYTES_PER_SAMPLE = 2 # 每采样点2字节
WS_HOST = "0.0.0.0"  # 监听所有网卡
WS_PORT = 8888       # WebSocket端口
SAVE_RESPONSE_AUDIO = False  # 是否保存AI回复音频（建议关闭，节省空间）

# LLM系统提示词（角色设定/风格等），可直接修改
with open(os.path.join(os.path.dirname(__file__), "system_prompt.md"), "r", encoding="utf-8") as f:
    LLM_SYSTEM_PROMPT = f.read()

# === AI/工具相关 ===
from llm_mcp import LLM_MCP
from tools.audio_to_text import AudioToText
from tools.edgeTTS import EdgeTTS

class WebSocketAudioServer:
    """
    WebSocket音频服务器核心类
    - 管理WebSocket连接
    - 音频收发与保存
    - ASR/LLM/TTS处理
    """

    def __init__(self):
        # 音频保存目录
        self.output_dir = os.path.join(os.path.dirname(__file__), "user_records")
        self.response_dir = os.path.join(os.path.dirname(__file__), "response_records")
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.response_dir, exist_ok=True)
        # 初始化AI相关组件
        self.llm_client = LLM_MCP(system_prompt=LLM_SYSTEM_PROMPT)
        self.asr = AudioToText()
        self.tts = EdgeTTS({"voice": "zh-CN-XiaoxiaoNeural"})
        self.save_response_audio = SAVE_RESPONSE_AUDIO

    async def handle_client(self, websocket):
        """
        处理每个ESP32客户端连接
        - 音频流接收
        - 控制事件处理
        - 录音结束后AI交互
        """
        client_ip = websocket.remote_address[0]
        print(f"\n🔗 新的客户端连接: {client_ip}")
        client_state = {
            "is_recording": False,
            "audio_buffer": bytearray(),
        }

        try:
            async for message in websocket:
                try:
                    # 音频流（二进制）
                    if isinstance(message, bytes):
                        if client_state["is_recording"]:
                            client_state["audio_buffer"].extend(message)
                        continue

                    # 控制消息（JSON）
                    data = json.loads(message)
                    event = data.get("event")

                    if event == "wake_word_detected":
                        print(f"🎉 [{client_ip}] 检测到唤醒词！")
                    elif event == "recording_started":
                        print(f"🎤 [{client_ip}] 开始录音...")
                        client_state["is_recording"] = True
                        client_state["audio_buffer"] = bytearray()
                    elif event == "recording_ended":
                        await self.process_recording_ended(client_ip, client_state, websocket)
                    elif event == "recording_cancelled":
                        print(f"⚠️ [{client_ip}] 录音取消")
                        client_state["is_recording"] = False
                        client_state["audio_buffer"] = bytearray()
                except json.JSONDecodeError as e:
                    print(f"❌ [{client_ip}] JSON解析错误: {e}")
                except Exception as e:
                    print(f"❌ [{client_ip}] 处理消息错误: {e}")
        except websockets.exceptions.ConnectionClosed:
            print(f"🔌 [{client_ip}] 客户端断开连接")
        except Exception as e:
            print(f"❌ [{client_ip}] 连接错误: {e}")

    async def process_recording_ended(self, client_ip, client_state, websocket):
        """
        录音结束后处理流程：
        1. 保存录音为WAV
        2. 语音识别（ASR）转文本
        3. 文本发送给LLM（支持工具调用）
        4. LLM回复转语音（TTS），实时返回ESP32
        """
        print(f"✅ [{client_ip}] 录音结束")
        client_state["is_recording"] = False

        if len(client_state["audio_buffer"]) == 0:
            await websocket.ping()
            return

        print(f"📊 [{client_ip}] 音频总大小: {len(client_state['audio_buffer'])} 字节 ({len(client_state['audio_buffer'])/2/SAMPLE_RATE:.2f}秒)")

        current_timestamp = datetime.now()
        wav_filename = self.save_wav(client_state["audio_buffer"], current_timestamp)
        print(f"✅ [{client_ip}] 音频已保存: {wav_filename}")

        recognized_text = self.asr.transcribe(wav_filename)
        print(f"📝 [{client_ip}] 识别文本: {recognized_text}")

        # 等待MCP服务器连接
        if not self.llm_client.sessions:
            print("等待MCP服务器连接...")
            await asyncio.sleep(2)

        response_text = await self.llm_client.process_query(recognized_text)
        print(f"🤖 [{client_ip}] 大模型回复: {response_text}")

        # TTS合成语音（AI回复音频不保存，只用于发送）
        tts_output_file = os.path.join(self.response_dir, f"response_{current_timestamp.strftime('%Y%m%d_%H%M%S')}_raw.wav")
        await self.tts.text_to_speech(response_text, tts_output_file)
        print(f"🔊 [{client_ip}] TTS音频已生成")

        # 转换为ESP32兼容PCM格式
        tts_pcm_file = os.path.join(self.response_dir, f"response_{current_timestamp.strftime('%Y%m%d_%H%M%S')}_pcm.wav")
        self.convert_to_pcm(tts_output_file, tts_pcm_file)
        print(f"🔊 [{client_ip}] TTS音频已转换为ESP32兼容格式")

        # 读取音频并发送给ESP32
        with open(tts_pcm_file, "rb") as f:
            tts_audio = f.read()
        await websocket.send(tts_audio)
        print(f"→ 已发送回复音频给 ESP32")

        # 仅在开关打开时保存回复音频（默认不开启）
        if self.save_response_audio:
            print(f"💾 回复音频已保存: {tts_output_file}, {tts_pcm_file}")
        else:
            # 删除AI回复音频文件，节省空间
            try:
                os.remove(tts_output_file)
                os.remove(tts_pcm_file)
            except Exception as e:
                print(f"⚠️ 删除回复音频文件失败: {e}")

        await websocket.ping()

    def save_wav(self, audio_buffer, timestamp):
        """
        保存音频为WAV文件
        参数:
            audio_buffer: 音频数据缓冲区
            timestamp: 时间戳
        返回:
            保存的文件路径
        """
        wav_filename = os.path.join(
            self.output_dir, f"recording_{timestamp.strftime('%Y%m%d_%H%M%S')}.wav"
        )
        with wave.open(wav_filename, "wb") as wav_file:
            wav_file.setnchannels(CHANNELS)
            wav_file.setsampwidth(BIT_DEPTH // 8)
            wav_file.setframerate(SAMPLE_RATE)
            wav_file.writeframes(bytes(audio_buffer))
        return wav_filename

    def convert_to_pcm(self, src_file, dst_file):
        """
        转换音频为ESP32兼容的PCM格式
        参数:
            src_file: 源音频文件路径
            dst_file: 目标音频文件路径
        """
        audio = AudioSegment.from_file(src_file)
        audio = audio.set_frame_rate(SAMPLE_RATE).set_channels(CHANNELS).set_sample_width(BIT_DEPTH // 8)
        audio.export(dst_file, format="wav")

    def get_local_ips(self):
        """
        获取本机所有可用的IP地址
        返回:
            list: IP地址列表
        """
        ips = []
        try:
            hostname = socket.gethostname()
            for info in socket.getaddrinfo(hostname, None):
                if info[0] == socket.AF_INET:
                    ip = info[4][0]
                    if ip not in ips and not ip.startswith("127."):
                        ips.append(ip)
            if not ips:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    s.connect(("8.8.8.8", 80))
                    ip = s.getsockname()[0]
                    if ip not in ips and not ip.startswith("127."):
                        ips.append(ip)
                except:
                    pass
                finally:
                    s.close()
            ips.append("127.0.0.1")
        except Exception as e:
            print(f"⚠️  获取本机IP地址失败: {e}")
            ips = ["127.0.0.1"]
        return ips

    async def start_server(self):
        """
        启动WebSocket服务器，监听ESP32连接
        """
        print("=" * 60)
        print("ESP32音频WebSocket服务器")
        print("=" * 60)
        local_ips = self.get_local_ips()
        print("可用的连接地址:")
        for ip in local_ips:
            print(f"  - ws://{ip}:{WS_PORT}")
        print("=" * 60)
        print("\n等待ESP32连接...\n")
        async with websockets.serve(self.handle_client, WS_HOST, WS_PORT):
            await asyncio.Future()

def main():
    """
    程序入口点
    - 初始化服务器
    - 启动事件循环
    - 连接MCP服务器
    """
    server = WebSocketAudioServer()
    try:
        async def run_server():
            await server.llm_client.connect_all_default_servers()
            await server.start_server()
        asyncio.run(run_server())
    except KeyboardInterrupt:
        print("\n\n⚠️  服务器已停止")

if __name__ == "__main__":
    main()

# 🎉 恭喜你看完了整个代码！
#
# 📚 学到了什么？
# 1. WebSocket服务器的基本实现
# 2. 异步编程的实际应用
# 3. 音频数据的处理和转换
# 4. 与AI API的实时通信
#
# 🚀 下一步可以尝试：
# 1. 修改语音风格（voice参数）
# 2. 添加更多功能（如多语言支持）
# 3. 优化音频质量（调整采样率）
# 4. 增加错误重试机制
#
# 💪 加油！你已经掌握了AI语音助手的核心技术！
