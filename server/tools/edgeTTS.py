import asyncio
import edge_tts


class EdgeTTS:
    def __init__(self, config):
        # 从嵌套的配置结构中提取 voice 参数
        self.voice = config.get("voice")

    async def text_to_speech(self, text, output_file):
        communicate = edge_tts.Communicate(text, self.voice)
        await communicate.save(output_file)
        return output_file
    

if __name__ == "__main__":
    config = {"voice": "zh-CN-XiaoxiaoNeural"}
    tts = EdgeTTS(config)
    text = "你是谁？"
    output_file = "output.wav"
    asyncio.run(tts.text_to_speech(text, output_file))
    print(f"语音已保存到 {output_file}")
