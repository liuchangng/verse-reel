"""合成极简古风 pad 背景音乐（零依赖，纯 stdlib）。

说明：
- 每个 mood 输出一段可循环的低音量铺底音（五声音阶和弦 pad，非旋律，避免"阴森"感）。
- 真实古风曲优先：若 server/assets/bgm/<mood>.mp3 已存在则跳过合成（支持用户放入真实曲目覆盖）。
- 产物经 ffmpeg(libmp3lame) 转 mp3；转码失败则保留 wav。

用法：python scripts/gen_bgm.py
"""
import math
import os
import struct
import subprocess
import wave

SR = 44100
PEAK = 0.18  # 低音量铺底，不抢旁白

# 五声音阶和弦 pad 配置：root=MIDI 根音, chord=半音叠加, bright=明亮度(高阶谐波权重)
MOODS = {
    "sad":      {"root": 48, "chord": [0, 3, 7, 12], "bright": 0.55, "dur": 32},  # 小三色彩，低沉
    "epic":     {"root": 55, "chord": [0, 4, 7, 12], "bright": 1.00, "dur": 30},  # 明亮，激昂
    "calm":     {"root": 50, "chord": [0, 4, 7, 14], "bright": 0.80, "dur": 34},  # 柔和
    "nostalgic":{"root": 53, "chord": [0, 3, 7, 12], "bright": 0.70, "dur": 32},  # 温暖思乡
    "general":  {"root": 48, "chord": [0, 4, 7, 12], "bright": 0.90, "dur": 30},  # 通用
}


def midi_freq(m: int) -> float:
    return 440.0 * (2.0 ** ((m - 69) / 12.0))


def render_pad(cfg: dict) -> bytes:
    dur = cfg["dur"]
    n = int(SR * dur)
    # 各和弦音的基频 + 谐波(1,2,3)，高阶谐波权重随 bright 衰减
    partials = []
    for semi in cfg["chord"]:
        f = midi_freq(cfg["root"] + semi)
        partials.append((f, 1.0))
        partials.append((f * 2, 0.35 * cfg["bright"]))
        partials.append((f * 3, 0.18 * cfg["bright"]))
    # 轻微音高微颤(vibrato)让 pad 不僵硬
    vib_rate = 0.12
    vib_depth = 1.5  # 半音
    samples = []
    for i in range(n):
        t = i / SR
        # 缓慢整体起伏 (tremolo) + 淡入淡出避免循环爆音
        env = 0.75 + 0.25 * math.sin(2 * math.pi * 0.07 * t)
        fade = min(1.0, t / 2.0) * min(1.0, (dur - t) / 2.0)
        s = 0.0
        for (f, amp) in partials:
            vib = 1.0 + (vib_depth / 100.0) * math.sin(2 * math.pi * vib_rate * t)
            s += amp * math.sin(2 * math.pi * f * vib * t)
        # 归一化 partials 幅度
        s /= len(cfg["chord"])
        s *= env * fade * PEAK
        samples.append(s)
    # 归一化峰值
    peak = max(abs(x) for x in samples) or 1.0
    scale = PEAK / peak
    buf = bytearray()
    for x in samples:
        v = int(max(-1.0, min(1.0, x * scale)) * 32767)
        buf += struct.pack("<h", v)
        buf += struct.pack("<h", v)  # 立体声相同
    return bytes(buf)


def main():
    out_dir = os.path.join(os.path.dirname(__file__), "..", "server", "assets", "bgm")
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    ffmpeg = "D:/Software/ffmpeg/bin/ffmpeg.exe"
    for mood, cfg in MOODS.items():
        mp3 = os.path.join(out_dir, f"{mood}.mp3")
        wav = os.path.join(out_dir, f"{mood}.wav")
        if os.path.exists(mp3) and os.path.getsize(mp3) > 0:
            print(f"[skip] {mood}.mp3 已存在（真实曲目优先）")
            continue
        pcm = render_pad(cfg)
        with wave.open(wav, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(SR)
            wf.writeframes(pcm)
        # 转 mp3
        try:
            subprocess.run(
                [ffmpeg, "-y", "-i", wav, "-codec:a", "libmp3lame", "-q:a", "4", mp3],
                check=True, capture_output=True, timeout=120,
            )
            os.remove(wav)
            print(f"[ok] {mood}.mp3 合成完成 ({cfg['dur']}s)")
        except Exception as e:
            print(f"[warn] {mood} mp3 转码失败，保留 wav: {e}")


if __name__ == "__main__":
    main()
