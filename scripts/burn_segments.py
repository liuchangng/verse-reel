"""逐镜以图定音 —— 独立合成脚本（不依赖后端进程，节省内存）。

用法：
    python scripts/burn_segments.py 1 3
    python scripts/burn_segments.py 1 3 --reuse     # 复用已编码 seg，仅重拼/烧录
默认处理 task 1 / 3。

流程（与 pipeline._build_segments + _burn_subtitles 保持一致）：
  1) 读取 task.storyboard / tts_segments.json（复用已生成的逐镜旁白 mp3）
  2) 每镜 {img_i + narration_i.mp3} 用「放大 1.12x + crop 时间平移」做内存安全 Ken Burns，
     合成 seg_{platform}_i.mp4（-shortest 严格贴合旁白时长）
  3) 镜间交叉淡入淡出（xfade）拼接为 base_{platform}.mp4，总时长 = Σ(旁白 - 转场重叠)
  4) 按时间轴写分段 SRT，烧录古风楷体字幕 + 低音量古风 BGM → final.mp4
  5) 回写 DB：subtitle_url / video_url / video_duration

注意：agnes source.mp4 音轨彻底不用；BGM 仅低音量铺底（volume=0.22）。
分辨率/平台：从 tasks.platform 读平台比例（抖音9:16=1080x1920 / 小红书3:4=1080x1440 / 横屏16:9=1920x1080）。
"""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import random

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "server" / "data" / "poems.db"
OUTPUT_DIR = ROOT / "server" / "data" / "output"
BGM_DIR = ROOT / "server" / "assets" / "bgm"
FFMPEG = "D:/Software/ffmpeg/bin/ffmpeg.exe"
FFPROBE = "D:/Software/ffmpeg/bin/ffprobe.exe"
SERVER_PUBLIC_URL = "http://localhost:8000"

# 字幕预设（与 app.services.subtitle.SUBTITLE_STYLES["kai"] 对齐）
SUBTITLE_FONT = "KaiTi"
SUBTITLE_OUTLINE = 2
SUBTITLE_SHADOW = 1
SUBTITLE_ALIGN = 2

# 水印（与 config.watermark_* 对齐；独立重烧脚本需自行叠加到分镜图）
WATERMARK_TEXT = "古诗词解说说"
WATERMARK_FONT = "C:/Windows/Fonts/simkai.ttf"   # 楷体；drawtext 须用 fontfile 绝对路径
WATERMARK_FONTSIZE_RATIO = 0.06   # 原0.03太小（1024图上仅31px），提至0.06=61px
WATERMARK_MARGIN_RATIO = 0.03
WATERMARK_ALPHA = 0.8             # 原0.65仍偏淡，提至0.8确保可见
WATERMARK_POSITION = "right_bottom"

# 转场（与 config.transition_* 对齐）
TRANSITION_ENABLED = True
TRANSITION_DURATION = 0.4

# 平台 → 最终合成分辨率（与 pipeline.FINAL_RESOLUTION 对齐）
FINAL_RESOLUTION = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "3:4": (1080, 1440),
    "4:3": (1440, 1080),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
}
PLATFORM_ASPECT = {
    "douyin": "9:16", "xiaohongshu": "3:4", "kuaishou": "9:16",
    "bili": "16:9",
}


def platform_resolution(platform: str) -> tuple[int, int]:
    aspect = PLATFORM_ASPECT.get(platform, "9:16")
    return FINAL_RESOLUTION.get(aspect, (1080, 1920))


def probe_duration(path: Path) -> float | None:
    try:
        p = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if p.returncode == 0 and p.stdout.strip():
            return float(p.stdout.strip())
    except Exception:
        pass
    return None


def probe_image_height(path: Path) -> int | None:
    try:
        p = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if p.returncode == 0 and p.stdout.strip():
            return int(p.stdout.strip())
    except Exception:
        pass
    return None


def ensure_watermark_images(out: Path) -> int:
    """对本地 img_{i}.png 叠加半透明文字水印生成 img_{i}_wm.png（与 pipeline 一致）。
    已存在则跳过。返回生成的 wm 文件数。独立重烧脚本不重新下载图片，故在此补叠加。
    """
    n = 0
    for p in sorted(out.glob("img_*.png")):
        if p.name.endswith("_wm.png"):
            continue
        wm = out / f"{p.stem}_wm.png"
        if wm.exists() and wm.stat().st_size > 0:
            n += 1
            continue
        h = probe_image_height(p) or 1024
        fs = max(18, int(h * WATERMARK_FONTSIZE_RATIO))
        margin = max(10, int(h * WATERMARK_MARGIN_RATIO))
        xy = (f"x={margin}:y=h-text_h-{margin}" if WATERMARK_POSITION == "left_bottom"
              else f"x=w-text_w-{margin}:y=h-text_h-{margin}")
        # 本机 drawtext 走 Fontconfig 且默认配置缺失，字体名方式会失败；
        # 必须用 fontfile 绝对路径（冒号转义 '\:'）。white@alpha 半透明，
        # 与 pipeline._apply_image_watermark 完全一致。
        ffont = WATERMARK_FONT.replace(":", "\\:")
        vf = (f"drawtext=fontfile='{ffont}':text='{WATERMARK_TEXT}':"
              f"fontcolor=white@{WATERMARK_ALPHA}:fontsize={fs}:{xy}")
        cmd = [FFMPEG, "-y", "-i", str(p).replace(chr(92), "/"),
               "-vf", vf, "-q:v", "2", str(wm).replace(chr(92), "/")]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode == 0 and wm.exists() and wm.stat().st_size > 0:
            n += 1
        else:
            print(f"  [warn] 水印失败 {p.name}: {r.stderr[-150:]}")
    return n


def select_bgm(style: str):
    if not BGM_DIR.exists():
        return None
    mood = style or "人生感悟"
    m = "general"
    if any(k in mood for k in ("忧", "悲", "伤", "愁", "凄", "苦", "怨", "哀")):
        m = "sad"
    elif any(k in mood for k in ("思", "乡", "怀", "忆", "念")):
        m = "nostalgic"
    elif any(k in mood for k in ("壮", "豪", "激", "志", "慷", "烈", "昂")):
        m = "epic"
    elif any(k in mood for k in ("静", "淡", "悠", "闲", "雅", "宁", "清")):
        m = "calm"
    cands = sorted(BGM_DIR.glob(f"{m}_*.mp3"))
    if not cands and m == "sad":
        cands = sorted(BGM_DIR.glob("nostalgic_*.mp3")) or sorted(BGM_DIR.glob("calm_*.mp3"))
    if not cands:
        for alt in ("nostalgic", "calm", "general", "epic"):
            cands = sorted(BGM_DIR.glob(f"{alt}_*.mp3"))
            if cands:
                break
    if not cands:
        cands = sorted(BGM_DIR.glob("*.wav"))
    if not cands:
        return None
    return str(cands[random.randrange(len(cands))])


def fmt_srt(sec: float) -> str:
    h = int(sec // 3600); m = int((sec % 3600) // 60); s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def write_segmented_srt(timeline, path: Path):
    lines = []
    for i, (st, en, text) in enumerate(timeline, 1):
        lines.append(str(i))
        lines.append(f"{fmt_srt(st)} --> {fmt_srt(en)}")
        lines.append(text)
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _plain_timeline(durs, texts):
    timeline, t = [], 0.0
    for d, tx in zip(durs, texts):
        timeline.append((t, t + d, tx))
        t += d
    return timeline, t


def _transition_timeline(durs, texts, trans):
    timeline, cum = [], 0.0
    for k, (d, tx) in enumerate(zip(durs, texts)):
        start = cum - k * trans
        timeline.append((start, start + d, tx))
        cum += d
    return timeline, cum - (len(durs) - 1) * trans


def _concat_copy(seg_files, base: Path) -> bool:
    listf = base.parent / "seg_list.txt"
    listf.write_text("\n".join(f"file '{p.replace(chr(92), '/')}'" for p in seg_files),
                     encoding="utf-8")
    r = subprocess.run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(listf),
                        "-c", "copy", str(base)], capture_output=True, text=True, timeout=300)
    return r.returncode == 0 and base.exists()


def _concat_xfade(seg_files, durs, trans, base: Path) -> bool:
    """交叉淡入淡出：视频 xfade 消除硬切；音频用 concat 滤镜 gapless 拼接
    （acrossfade 在本机合并滤镜图中会丢失输出标签，已弃用）。
    durs 须为帧量化（25fps→0.04s 网格）时长，offset 才与 ffmpeg 实际帧对齐。"""
    n = len(seg_files)
    if n == 1:
        import shutil
        shutil.copy(seg_files[0], base)
        return base.exists()
    vparts = []
    prev_v = "[0:v]"
    out_v = "v1"
    acc_end = durs[0]
    for k in range(1, n):
        off = max(acc_end - trans, 0.05)
        vparts.append(f"{prev_v}[{k}:v]xfade=transition=fade:duration={trans:.2f}:offset={off:.3f}[{out_v}]")
        prev_v = f"[{out_v}]"
        out_v = f"v{k+1}"
        acc_end = acc_end - trans + durs[k]
    # 音频 gapless 拼接（concat 滤镜，稳健且不会在合并图中缺失输出标签）
    ainputs = "".join(f"[{k}:a]" for k in range(n))
    afc = f"{ainputs}concat=n={n}:v=0:a=1[aout]"
    fc = ";".join(vparts) + ";" + afc
    inputs = []
    for f in seg_files:
        inputs += ["-i", str(f).replace(chr(92), "/")]
    # -map 视频用最后一次 xfade 实际产出标签 v{n-1}；音频用 concat 的 [aout]
    cmd = [FFMPEG, "-y", *inputs, "-filter_complex", fc,
           "-map", f"[v{n-1}]", "-map", "[aout]",
           "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", str(base)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        print(f"  [ERR] xfade 失败: {r.stderr[-300:]}")
        return False
    return base.exists() and base.stat().st_size > 0


def build_segments(task_id: int, segments: list[dict], out: Path,
                    W: int, H: int, platform: str = "douyin",
                    reuse_seg: bool = False) -> dict:
    """逐镜合成 seg_{platform}_{i}.mp4（内存安全 Ken Burns 平移），镜间 xfade 拼为 base_{platform}.mp4。
    seg/base 加平台前缀，确保多平台 + --reuse 时各平台独立分辨率不串用。"""
    # 确保分镜水印图存在（独立重烧时本地已下载原图，此处补叠加，与 pipeline 一致）
    n_wm = ensure_watermark_images(out)
    if n_wm:
        print(f"  [wm] task{task_id} 水印图就绪 {n_wm} 张")
    seg_files, durs, texts = [], [], []
    for s in segments:
        idx = s["index"]
        wm = out / f"img_{idx}_wm.png"
        img = wm if wm.exists() else out / f"img_{idx}.png"
        aud = Path(s["path"])
        if not (img.exists() and aud.exists()):
            print(f"  [warn] task{task_id} shot {idx}: 缺图或旁白，跳过")
            continue
        dur = probe_duration(aud) or max(float(s.get("duration") or 3.0), 1.0)
        seg = out / f"seg_{platform}_{idx}.mp4"
        if reuse_seg and seg.exists() and seg.stat().st_size > 0:
            sd = probe_duration(seg)
            if sd and abs(sd - dur) <= 0.5:
                seg_files.append(str(seg))
                durs.append(dur)
                texts.append(s.get("text", ""))
                print(f"  [reuse] task{task_id} shot {idx} seg 复用 ({dur:.1f}s)")
                continue
            else:
                print(f"  [re-encode] task{task_id} shot {idx} seg 时长不符"
                      f"({sd:.2f}s vs {dur:.2f}s)，重新编码")
        vf = (f"scale={int(W*1.12)}:{int(H*1.12)}:"
              f"force_original_aspect_ratio=increase,"
              f"crop={W}:{H}:"
              f"x='(iw-{W})/2*(1+0.45*sin(2*PI*t/{dur:.2f}))':"
              f"y='(ih-{H})/2*(1+0.45*cos(2*PI*t/{dur:.2f}))',"
              f"setsar=1,format=yuv420p")
        cmd = [FFMPEG, "-y", "-loop", "1", "-i", str(img), "-i", str(aud),
               "-shortest", "-vf", vf,
               "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", str(seg)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not seg.exists() or seg.stat().st_size == 0:
            vf2 = (f"scale={int(W*1.12)}:{int(H*1.12)}:"
                   f"force_original_aspect_ratio=increase,"
                   f"crop={W}:{H},setsar=1,format=yuv420p")
            cmd2 = [FFMPEG, "-y", "-loop", "1", "-i", str(img), "-i", str(aud),
                    "-shortest", "-vf", vf2,
                    "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", str(seg)]
            r = subprocess.run(cmd2, capture_output=True, text=True, timeout=300)
        if r.returncode == 0 and seg.exists() and seg.stat().st_size > 0:
            seg_files.append(str(seg))
            durs.append(dur)
            texts.append(s.get("text", ""))
            print(f"  [ok] task{task_id} shot {idx} seg 合成 ({dur:.1f}s)")
        else:
            print(f"  [ERR] task{task_id} shot {idx} seg 合成失败: {r.stderr[-200:]}")
    if not seg_files:
        return {"ok": False}
    # 帧量化：seg 按 25fps 编码（0.04s 网格）；xfade 链式 offset 用帧量化时长
    # 才与 ffmpeg 实际帧对齐，消除 21 段长链的累积漂移越界。
    FPS = 25
    durs = [max(0.04, round(d * FPS) / FPS) for d in durs]
    base = out / f"base_{platform}.mp4"
    n = len(seg_files)
    trans = TRANSITION_DURATION if (TRANSITION_ENABLED and n > 1) else 0.0
    if trans > 0:
        trans = min(trans, max(min(durs) * 0.4, 0.1))
        timeline, total = _transition_timeline(durs, texts, trans)
        ok = _concat_xfade(seg_files, durs, trans, base)
        if not ok:
            print("  [warn] xfade 失败，回退硬切")
            timeline, total = _plain_timeline(durs, texts)
            ok = _concat_copy(seg_files, base)
    else:
        timeline, total = _plain_timeline(durs, texts)
        ok = _concat_copy(seg_files, base)
    if not ok:
        return {"ok": False}
    print(f"  [ok] task{task_id} base 合成完成: {n} 段，总时长 {total:.1f}s (trans={trans:.2f})")
    return {"ok": True, "base": str(base), "timeline": timeline, "duration": round(total, 2)}


def burn_subtitles(task_id: int, style: str, segs: dict, out: Path,
                   W: int, H: int, platform: str = "douyin") -> str:
    """烧录分段字幕 + 低音量古风 BGM → final_{platform}.mp4（多平台模式）或 final.mp4。

    水印已在分镜图(img_{i}_wm.png)阶段叠加，经 seg→base 已存在于画面，
    故此处不再二次叠加 drawtext 水印（避免与 pipeline 双重水印不一致）。
    """
    base = out / f"base_{platform}.mp4"
    if not base.exists() or base.stat().st_size == 0:
        print(f"  [ERR] task{task_id} base_{platform}.mp4 不存在")
        return ""
    # 多平台模式：非默认平台用 final_{platform}.mp4 后缀避免覆盖
    suffix = f"_{platform}" if platform != "douyin" else ""
    final = out / f"final{suffix}.mp4"
    srt = out / "subtitle.srt"
    timeline = segs.get("timeline") or []
    if not timeline:
        t = 0.0
        for s in segs.get("segments", []):
            d = float(s.get("duration", 3.0))
            timeline.append((t, t + d, s.get("text", "")))
            t += d
    write_segmented_srt(timeline, srt)
    # 权威时长 = build_segments 累加的 Σ(旁白 - 转场重叠)
    total = segs.get("duration") or probe_duration(base) or 30.0
    # 字号随分辨率缩放（默认3%→抖音约58px/B站56px/小红书43px；0.055过大致遮满画面）
    fs = max(56, min(120, int(H * 0.03)))
    # 底部边距：默认5%→96px（原10%=192px仍偏高，30%=576px推到图像区不可见）
    mv = max(60, int(H * 0.05))
    # 描边加粗+阴影加深：确保白字在任何背景上可读
    force_style = (f"FontName={SUBTITLE_FONT},FontSize={fs},"
                   f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                   f"Outline=3,Shadow=2,"
                   f"Alignment={SUBTITLE_ALIGN},MarginV={mv}")
    srt_esc = str(srt).replace("\\", "/").replace(":", "\\:")
    base_fwd = str(base).replace("\\", "/")
    bgm = select_bgm(style)
    if bgm:
        bgm_fwd = str(bgm).replace("\\", "/")
        cmd = [FFMPEG, "-y", "-i", base_fwd, "-stream_loop", "-1", "-i", bgm_fwd,
               "-filter_complex",
               (f"[0:v]subtitles='{srt_esc}':force_style='{force_style}'[v];"
                f"[0:a]volume=1.0[a0];[1:a]volume=0.22[a1];"
                f"[a0][a1]amix=inputs=2:duration=first[aout]"),
               "-map", "[v]", "-map", "[aout]", "-t", f"{total:.2f}",
               "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", str(final)]
    else:
        cmd = [FFMPEG, "-y", "-i", base_fwd,
               "-vf", f"subtitles='{srt_esc}':force_style='{force_style}'",
               "-map", "0:v:0", "-map", "0:a:0", "-t", f"{total:.2f}",
               "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", str(final)]
    print(f"  [burn] task{task_id} 烧字幕+BGM (bgm={bool(bgm)}, 水印已在分镜图阶段叠加, dur={total:.1f}s)")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not final.exists() or final.stat().st_size == 0:
        print(f"  [ERR] task{task_id} 烧录失败: {r.stderr[-300:]}")
        return ""
    final_url = f"{SERVER_PUBLIC_URL}/outputs/task_{task_id}/final{suffix}.mp4"
    return final_url


# 默认全平台输出（可在命令行用 --platforms douyin,bili 覆盖）
DEFAULT_PLATFORMS = ["douyin", "bili", "xiaohongshu"]


def main():
    args = sys.argv[1:]
    reuse = "--reuse" in args
    # 支持命令行指定平台: --platforms douyin,bili,xiaohongshu
    platforms = DEFAULT_PLATFORMS
    for i, a in enumerate(args):
        if a == "--platforms" and i + 1 < len(args):
            platforms = [p.strip() for p in args[i + 1].split(",") if p.strip()]
            break
    ids = [int(x) for x in args if x.lstrip("-").isdigit()] or [1, 3]
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    for tid in ids:
        row = con.execute("SELECT storyboard, style, platform, video_url FROM tasks WHERE id=?",
                          (tid,)).fetchone()
        if not row:
            print(f"task {tid}: 不存在，跳过")
            continue
        out = OUTPUT_DIR / f"task_{tid}"
        seg_json = out / "tts_segments.json"
        if not seg_json.exists():
            print(f"task {tid}: 无 tts_segments.json（旁白未生成），跳过")
            continue
        segments = json.loads(seg_json.read_text(encoding="utf-8"))
        # 多平台循环：TTS/图片/SRT 复用，仅 seg→base→final 按分辨率分别生成
        platform_urls = {}
        for plat in platforms:
            W, H = platform_resolution(plat)
            print(f"\n=== task {tid}: {len(segments)} 镜, style={row['style']}, "
                  f"platform={plat}, {W}x{H} (reuse={reuse}) ===")
            segs = build_segments(tid, segments, out, W, H, platform=plat,
                                  reuse_seg=reuse)
            if not segs.get("ok"):
                print(f"task {tid}/{plat}: 片段合成失败，跳过此平台")
                continue
            final_url = burn_subtitles(tid, row["style"] or "人生感悟", segs, out, W, H,
                                      platform=plat)
            if not final_url:
                print(f"task {tid}/{plat}: 烧录失败，跳过此平台")
                continue
            platform_urls[plat] = final_url
            print(f"task {tid}/{plat}: ✅ final.mp4 完成，时长 {segs.get('duration')}s -> {final_url}")
        # 更新 DB：多平台 URL 用 JSON 存储或取第一个作为主视频
        primary = platform_urls.get(row["platform"] or "douyin", "")
        all_urls = json.dumps(platform_urls, ensure_ascii=False) if len(platform_urls) > 1 else None
        con.execute("UPDATE tasks SET video_url=?, video_duration=? WHERE id=?",
                    (primary, segs.get("duration") if segs else None, tid))
        con.commit()
        if len(platform_urls) > 1:
            print(f"\ntask {tid}: ✅ 全部平台完成 -> {list(platform_urls.keys())}")
    con.close()


if __name__ == "__main__":
    main()
