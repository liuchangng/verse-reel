"""把 data/ 下掉落的参考音片段接入 voice_presets.json（学习强国/自录均可）。

用法（用户在 学习强国 APP 剪好 6~10s 清晰朗读片段后）：
  1. 命名为 data/ref_<preset_id>.wav（如 data/ref_fangming.wav）
  2. 参考文字写到 data/ref_<preset_id>.txt（即该片段念的文字，作 CosyVoice2 prompt_text）
  3. 运行：python scripts/wire_refs.py
本脚本自动回填 ref_wav + prompt_text，并置 has_ref_audio=True；缺失文件/文字会给提示。
"""
import json
import pathlib

DATA = pathlib.Path("data")
PRESETS = DATA / "voice_presets.json"


def main():
    d = json.loads(PRESETS.read_text(encoding="utf-8"))
    by_id = {p["id"]: p for p in d["presets"]}
    wired, missing = [], []
    for wav in sorted(DATA.glob("ref_*.wav")):
        pid = wav.stem[len("ref_"):]
        if pid not in by_id:
            print(f"跳过：ref_{pid}.wav 不匹配任何 preset")
            continue
        txt = wav.with_suffix(".txt")
        prompt = txt.read_text(encoding="utf-8").strip() if txt.exists() else ""
        by_id[pid]["ref_wav"] = str(wav)
        by_id[pid]["prompt_text"] = prompt
        wired.append((pid, str(wav), "有文字" if prompt else "无文字(建议补)"))
    PRESETS.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已接线 preset：")
    for w in wired:
        print("  ", w)
    print(f"共接线 {len(wired)} 个；仍缺参考音频的 preset：",
          [p["id"] for p in d["presets"] if not p.get("ref_wav")] or "无")


if __name__ == "__main__":
    main()
