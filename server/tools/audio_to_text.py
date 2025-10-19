import os
from funasr import AutoModel
from funasr.utils.postprocess_utils import rich_transcription_postprocess
from dotenv import load_dotenv


load_dotenv()

class AudioToText:
    def __init__(self, model_dir = os.getenv("ASR_MODEL_DIR")):
        # model_dir: 模型文件所在目录
        self.model = AutoModel(
                model=model_dir,
                vad_model="fsmn-vad",
                vad_kwargs={"max_single_segment_time": 30000},
                device="cuda:0",
                hub="hf",
        )

    def transcribe(self, audio_path, language="auto", use_itn=True, batch_size_s=60, merge_vad=True, merge_length_s=15):
        """
        audio_path: 音频文件路径
        返回：识别到的文本字符串
        """
        res = self.model.generate(
            input=audio_path,
            cache={},
            language=language,
            use_itn=use_itn,
            batch_size_s=batch_size_s,
            merge_vad=merge_vad,
            merge_length_s=merge_length_s,
        )
        text = rich_transcription_postprocess(res[0]["text"])
        return text

# 用法示例
if __name__ == "__main__":
    audio_file = "output.wav"  # 修改为你的音频文件
    at = AudioToText()
    text = at.transcribe(audio_file)
    print("识别结果：", text)