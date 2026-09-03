"""旁白语速标定：实测历史 TTS 段的「字/秒」，校准字数预算系数

设计文档：docs/design/2026-09-03-video-communication-design.md（video-comm 决策 3，2026-09-03 拍板）

动机：
- S 快档文案字数预算（80–130 字 → 25–40s）依赖"中文旁白 ≈ X 字/秒"的系数；
  现默认 3.5 是行业二手经验值（config.speech_rate_cps），需用本机真实 TTS 产物标定。
- 完全离线：扫历史任务 output/task_*/tts_segments.json，对每段旁白
  （text + path）用 ffprobe 实测音频时长，字/秒 = 有效字数 / 实测秒数。
  不重新合成 TTS、不依赖网络/引擎——样本来自历次真实成片，天然代表当前音色/语速。

过滤规则（防脏样本污染）：
- 音频文件缺失或 0 字节 → 跳过
- 静音兜底段（TTS 失败生成的 anullsrc）无语音：特征 = 文件极小（<15KB）且时长 >2s → 跳过
- 字/秒落在 [1.5, 8] 之外 → 视为异常/非纯中文段 → 跳过
- 记录时长（tts_segments.json 的 duration）与 ffprobe 实测偏差 >15% → 记录可能是估计值 → 跳过

用法：
    python scripts/calibrate_speech_rate.py                # 只统计并打印报告
    python scripts/calibrate_speech_rate.py --apply        # 统计后把建议系数写回 server/.env 的 speech_rate_cps
    python scripts/calibrate_speech_rate.py --detail       # 打印每任务/每段的明细
"""
import sys
import json
import argparse
import subprocess
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover - 非 UTF-8 环境忽略
    pass

from app.config import settings, PROJECT_ROOT  # noqa: E402

# 语速合理区间（字/秒）：<1.5 多为长停顿/静音残留，>8 基本不可能是中文朗读
CPS_MIN, CPS_MAX = 1.5, 8.0
# 静音兜底段特征：文件极小且时长不小
SILENCE_SIZE_BYTES = 15 * 1024
# 记录时长与实测时长允许偏差（防"估计值"混入）
DUR_TOLERANCE = 0.15
# 有效字数：中文字符 + 数字 + 字母（标点/空白不计）
_CHAR_RE = re.compile(r"[\u4e00-\u9fff0-9a-zA-Z]")


def _ffprobe_duration(path: Path) -> float | None:
    """ffprobe 实测音频时长（秒），失败返回 None。"""
    try:
        ffprobe = settings.ffmpeg_path.replace("ffmpeg.exe", "ffprobe.exe")
        p = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if p.returncode == 0 and p.stdout.strip():
            return float(p.stdout.strip())
    except Exception:
        pass
    return None


def _char_count(text: str) -> int:
    """有效字数（不含标点/空白）。"""
    return len(_CHAR_RE.findall(text or ""))


def collect_samples(output_dir: Path, detail: bool = False) -> tuple[list, int]:
    """扫 output_dir 下全部 task_*/tts_segments.json，返回 (样本, 剔除数)。

    样本: dict(task, idx, text_preview, chars, dur_record, dur_probe, cps, engine, path)
    """
    samples, skipped = [], 0
    tasks = sorted(output_dir.glob("task_*/tts_segments.json"))
    for segf in tasks:
        task_name = segf.parent.name
        try:
            segs = json.loads(segf.read_text(encoding="utf-8"))
        except Exception:
            skipped += 1
            continue
        if not isinstance(segs, list):
            skipped += 1
            continue
        for s in segs:
            text = (s.get("text") or "").strip()
            chars = _char_count(text)
            if chars < 4:
                skipped += 1
                continue
            p = Path(s.get("path") or "")
            if not p.is_absolute():
                p = segf.parent / p
            if not p.exists() or p.stat().st_size == 0:
                skipped += 1
                continue
            size = p.stat().st_size
            dur_record = float(s.get("duration") or 0.0)
            dur = _ffprobe_duration(p)
            # 静音兜底段：文件极小且时长 >2s → 无实际语音
            if size < SILENCE_SIZE_BYTES and dur and dur > 2.0:
                skipped += 1
                continue
            if not dur or dur <= 0.3:
                skipped += 1
                continue
            # 记录时长（可能是 len/4.0 估计值）与实测偏差过大 → 记录不可信 → 跳过
            if dur_record > 0 and abs(dur - dur_record) / dur_record > DUR_TOLERANCE:
                skipped += 1
                continue
            cps = chars / dur
            if not (CPS_MIN <= cps <= CPS_MAX):
                skipped += 1
                continue
            samples.append({
                "task": task_name, "idx": s.get("index"), "chars": chars,
                "dur": round(dur, 2), "cps": round(cps, 2),
                "engine": s.get("engine") or "unknown",
                "preview": text[:24],
            })
            if detail:
                print(f"  {task_name}[{s.get('index')}] chars={chars} dur={dur:.2f}s "
                      f"cps={cps:.2f} eng={samples[-1]['engine']:<10} {text[:24]}")
    return samples, skipped


def percentile(sorted_vals: list[float], q: float) -> float:
    """线性插值分位数（sorted_vals 升序）。"""
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def main() -> int:
    ap = argparse.ArgumentParser(description="旁白语速标定（离线扫 tts_segments.json）")
    ap.add_argument("--apply", action="store_true",
                    help="把建议系数写回 server/.env 的 speech_rate_cps")
    ap.add_argument("--detail", action="store_true", help="打印每段明细")
    args = ap.parse_args()

    output_dir = Path(settings.output_dir)
    if not output_dir.exists():
        print(f"[错误] 输出目录不存在: {output_dir}")
        return 1

    samples, skipped = collect_samples(output_dir, detail=args.detail)
    if not samples:
        print(f"[无样本] output_dir={output_dir} 无有效 TTS 段（跳过 {skipped} 条）")
        return 1

    # 分组（按 engine）与总体统计
    by_engine: dict[str, list] = {}
    for s in samples:
        by_engine.setdefault(s["engine"], []).append(s["cps"])
    print(f"\n==== 旁白语速标定报告（video-comm 决策 3）====")
    print(f"样本任务数: {len(set(s['task'] for s in samples))}  有效段落: {len(samples)}  剔除: {skipped}")

    def _stat(name: str, vals: list[float]):
        sv = sorted(vals)
        mean = sum(sv) / len(sv)
        print(f"  [{name:<10}] n={len(sv):>3}  均值={mean:.3f}  中位={percentile(sv, .5):.3f}  "
              f"P25={percentile(sv, .25):.3f}  P75={percentile(sv, .75):.3f}  "
              f"min={sv[0]:.2f}  max={sv[-1]:.2f}")
        return percentile(sv, .5)

    print("\n[按引擎分组]")
    for eng, vals in sorted(by_engine.items(), key=lambda kv: -len(kv[1])):
        _stat(str(eng), vals)
    print("\n[总体]")
    all_cps = [s["cps"] for s in samples]
    med = _stat("all", all_cps)

    # 建议：样本最多引擎的中位数（若该引擎占比足够），否则总体中位数
    main_eng, main_vals = max(by_engine.items(), key=lambda kv: len(kv[1]))
    suggested = percentile(sorted(main_vals), .5) if len(main_vals) >= 0.6 * len(samples) else med
    print(f"\n[建议] speech_rate_cps = {suggested:.2f}（依据: 主引擎 {main_eng} 中位数，"
          f"覆盖 {len(main_vals)}/{len(samples)} 样本）")
    print(f"  换算: S 档 80 字 ≈ {80 / suggested:.0f}s | 130 字 ≈ {130 / suggested:.0f}s "
          f"（目标窗 25–40s）")
    print(f"  现行默认 {settings.speech_rate_cps:.2f}（config 经验值）")

    if args.apply:
        env_path = PROJECT_ROOT / "server" / ".env"
        if not env_path.exists():
            print(f"[--apply 失败] 未找到 {env_path}，请手动添加 speech_rate_cps={suggested:.2f}")
            return 1
        lines = env_path.read_text(encoding="utf-8").splitlines()
        key = "speech_rate_cps"
        new_line = f"{key}={suggested:.2f}"
        out, replaced = [], False
        for ln in lines:
            if ln.strip().startswith(key + "="):
                out.append(new_line)
                replaced = True
            else:
                out.append(ln)
        if not replaced:
            out.append(new_line)
        env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
        print(f"[--apply 完成] {env_path} 已写 {new_line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
