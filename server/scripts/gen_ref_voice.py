"""生成 CosyVoice2 零样本克隆用的参考音频（wav，24kHz 单声道）。

用法:
  ./.venv/Scripts/python.exe scripts/gen_ref_voice.py [--text 文本] [--voice 音色] [--out 输出wav]

参考音频要求:真人/清晰语音，6~10 秒最佳；prompt_text 必须与音频内容一致。
本脚本用 edge-tts 合成一段已知文本的中文语音作为占位参考，方便快速验证
zero-shot 代码路径；正式使用请替换为自己的录音（直接放 wav/mp3 并改 .env）。
"""
import argparse
import asyncio
from pathlib import Path

import torch
import torchaudio
import soundfile as sf

import edge_tts

BASE = Path(__file__).resolve().parent.parent  # server/
DEFAULT_TEXT = "春眠不觉晓，处处闻啼鸟。夜来风雨声，花落知多少。"
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"  # 温柔女声，适合诗词朗读
DEFAULT_OUT = str(BASE / "data" / "ref_voice.wav")


async def main(text: str, voice: str, out_path: str):
    comm = edge_tts.Communicate(text, voice)
    buf = b""
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            buf += chunk["data"]
    if not buf:
        raise RuntimeError("edge-tts 未返回音频（检查网络/区域）")

    tmp_mp3 = BASE / "data" / "_ref_tmp.mp3"
    tmp_mp3.write_bytes(buf)

    # soundfile(libsndfile 1.2.2) 直接读 mp3，免 FFmpeg
    speech, sr = sf.read(str(tmp_mp3), dtype="float32", always_2d=True)
    speech = torch.from_numpy(speech).mean(dim=1, keepdim=True)  # 转单声道
    if sr != 24000:
        speech = torchaudio.transforms.Resample(orig_freq=sr, new_freq=24000)(speech)

    sf.write(out_path, speech.squeeze().numpy(), 24000, format="WAV")
    print(f"参考音频已生成: {out_path}  (原始 {sr}Hz -> 24000Hz, 样本数 {speech.shape[-1]})")
    print(f"对应 prompt_text: {text}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default=DEFAULT_TEXT)
    ap.add_argument("--voice", default=DEFAULT_VOICE)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()
    asyncio.run(main(args.text, args.voice, args.out))
