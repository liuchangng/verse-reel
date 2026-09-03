"""[临时占位] 用 edge-tts 生成参考音，证明「选不同 preset → 出不同音色」链路通畅。

占位说明：用户指定最终音源为学习强国「美声雅韵」免费音频（APP 内，沙箱无法自动下载）。
本脚本仅生成临时占位参考音，让自动选声链路此刻即可听出差异；用户把学习强国片段
命名为 data/ref_<id>.wav（+ 同名 .txt 参考文字）丢进 data/ 后，运行 wire_refs.py
即用真实片段覆盖此处占位，机制完全相同。
"""
import asyncio
import os

import edge_tts
import soundfile as sf

# 占位覆盖「学习强国免费有的 3 位里先验证 2 位清晰性别」：方明(男) / 虹云(女)
REFS = {
    "data/ref_fangming.wav": ("zh-CN-YunyangNeural", "大江东去，浪淘尽，千古风流人物。"),
    "data/ref_hongyun.wav": ("zh-CN-XiaoxiaoNeural", "关关雎鸠，在河之洲。窈窕淑女，君子好逑。"),
}


async def synth(voice: str, text: str, wav_path: str):
    comm = edge_tts.Communicate(text, voice)
    mp3 = b""
    async for chunk in comm.stream():
        if chunk.get("type") == "audio":
            mp3 += chunk["data"]
    if not mp3:
        raise RuntimeError(f"edge-tts 未返回音频: {voice}")
    tmp = wav_path + ".mp3"
    with open(tmp, "wb") as f:
        f.write(mp3)
    data, sr = sf.read(tmp)
    if data.ndim > 1:
        data = data.mean(axis=1)
    sf.write(wav_path, data, sr)
    with open(wav_path.replace(".wav", ".txt"), "w", encoding="utf-8") as f:
        f.write(text)
    os.remove(tmp)
    print(f"wrote {wav_path}: sr={sr}, dur={len(data) / sr:.2f}s, text={text!r}")


async def main():
    for wav_path, (voice, text) in REFS.items():
        await synth(voice, text, wav_path)


if __name__ == "__main__":
    asyncio.run(main())
