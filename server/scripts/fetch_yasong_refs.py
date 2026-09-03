"""从雅诵网(yasong.net)批量抓取名人纯人声朗读片段，作 CosyVoice2 参考音。

流程（每个 preset）：
  1. 抓 reciter 的 tag 页 -> 取前若干帖子 URL
  2. 逐个帖子抓正文 -> 找 <audio src="...m4a/.mp3"> 或 media.yasong 直链
  3. 过滤：文件名含「配乐」的跳过（带背景乐，污染参考音），取首个纯人声段
  4. 带 Referer 下载 -> ffmpeg 转 24k 单声道 wav -> 截安全段(跳过开头 3s 防 intro/旁白)
  5. 从帖子正文抽朗诵文本作 prompt_text
  6. 写出 data/ref_<preset_id>.wav + .txt

用法：
  python scripts/fetch_yasong_refs.py
仅作个人项目参考音(输出为新合成语音，非再分发原音频)。
"""
import json
import re
import subprocess
import sys
import pathlib
import urllib.request
import urllib.parse
import urllib.error

SERVER = pathlib.Path(__file__).resolve().parent.parent
DATA = SERVER / "data"
FFMPEG = r"D:\Software\ffmpeg\bin\ffmpeg.exe"
FFPROBE = r"D:\Software\ffmpeg\bin\ffprobe.exe"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# preset_id -> 雅诵网 reciter tag slug（已 URL 编码）
TARGETS = {
    "hongyun":   "%e8%99%b9%e4%ba%91",   # 虹云
    "pucunxin":  "%e6%bf%ae%e5%ad%98%e6%98%95",  # 濮存昕
    "qiaozhen":  "%e4%b9%94%e6%a6%9b",   # 乔榛
    "jiaohuang": "%e5%be%90%e6%b6%9b",   # 徐涛（preset=焦晃/徐涛）
    "kangzhuang": "%e7%9e%bf%e5%bc%a6%e5%92%8c",  # 瞿弦和（preset=康庄/瞿弦和）
    "yakun":     "%e9%9b%85%e5%9d%a4",   # 雅坤
    "siqin":     "%e8%91%a3%e5%8d%bf",   # 董卿（preset=斯琴高娃/董卿）
    "dingjianhua": "%e4%b8%81%e5%bb%ba%e5%8d%8e",  # 丁建华
    # —— 第二轮补齐：雅诵网无对应 tag，但 ?s=名字 搜索有结果 ——
    "xiaoxiong":  "%e8%82%96%e9%9b%84",   # 肖雄
    "kanghui":    "%e5%ba%b7%e8%be%89",   # 康辉
    "xuefei":     "%e8%96%9b%e9%a3%9e",   # 薛飞
    "yaoxijuan":  "%e5%a7%9a%e9%94%a1%e5%a8%9f",  # 姚锡娟
}

# 这些 preset 雅诵网无 tag 聚合页，改用 ?s=名字 搜索路由抓帖子
SEARCH = {"xiaoxiong", "kanghui", "xuefei", "yaoxijuan"}

TRIM_START = 3.0   # 跳过开头，避开站点 intro/旁白
TRIM_DUR = 10.0    # 参考音长度


def http_get(url: str, binary: bool = False, referer: str = None, timeout: int = 30):
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        return data if binary else data.decode("utf-8", errors="ignore")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"  [http] {url} -> {e}")
        return None


def get_duration(wav: pathlib.Path) -> float:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(wav)],
        capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


BIO_KW = re.compile(r"(出生|毕业|年生|朗诵艺术|广播|电视台|学院教授|主持人|主任|籍贯|协会会员|一级演员|艺术团|文工团|配音演员|考入|受聘|担任|享受政府|家协会|译制|指导播音)")


def extract_text(html: str) -> str:
    """取 article 正文纯文本。"""
    m = re.search(r"<article[^>]*>(.*?)</article>", html, re.S)
    body = m.group(1) if m else html
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", body, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", body)
    text = re.sub(r"&[a-z]+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"默认分类\s*", "", text)
    text = re.sub(r"^\s*[^。！？]*?\d{1,2}\s*月\s*\d{1,2}[,，]?\s*\d{4}\s*", "", text)
    text = re.sub(r"(作者|[诵读朗][者读诵]?)[：:]\s*\S*\s*", "", text)
    text = re.sub(r"[《》（】]\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def pick_prompt(text: str) -> str:
    """丢掉开头的朗读者小传(旁白)，取朗诵正文前 ~50 字作 prompt_text。"""
    parts = re.split(r"[。！？!?]", text)
    buf = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if not buf and BIO_KW.search(p):
            continue  # 跳过开头小传句
        buf.append(p)
        if len("".join(buf)) >= 40:
            break
    return "".join(buf)[:50]


WIDGET_KW = re.compile(
    r"(我要打分|我要评分|用微信扫描并分享|浏览量|默认分类|大音希声|"
    r"相关推荐|console\.log|var audios|扫码|扫描并分享)"
)


def strip_widget(text: str) -> str:
    """清掉帖子页里的评分/分享控件噪声。"""
    return WIDGET_KW.sub(" ", text)


def clean_cjk_len(text: str) -> int:
    """去掉控件词后、仅统计中文标点的长度，用于判断正文是否含朗诵词。"""
    t = strip_widget(text)
    return len(re.sub(r"[^一-鿿。！？，、；：]", "", t))


def poem_title_from(title: str, name: str) -> str:
    """从帖子标题提取诗名，作无正文时的 prompt_text 兜底。"""
    t = title.split("|")[0].strip()  # 去掉 " | 雅诵网"
    t = re.sub(rf"^{re.escape(name)}\s*[-－:：]?\s*", "", t)  # 去掉 "肖雄-"
    t = re.sub(r"^(朗诵|诵读|朗读)\s*[:：]?", "", t)
    t = re.sub(r"[《》（】)、『』“”\"]", "", t)
    t = re.sub(r"\s+", "", t)
    return (t[:30] if t else name)


def post_title(html: str) -> str:
    m = re.search(r"<title>([^<]+)</title>", html)
    return m.group(1) if m else ""


def find_audio_url(html: str) -> str | None:
    # <audio ... src="https://media.yasong.net/...m4a/.mp3">
    m = re.search(r'src="(https://media\.yasong\.net/[^"]+\.(?:m4a|mp3))"', html)
    if m:
        return m.group(1)
    # 兜底：任意 media.yasong ...m4a/mp3
    m = re.search(r"(https://media\.yasong\.net/[^\"')\s]+\.(?:m4a|mp3))", html)
    return m.group(1) if m else None


def find_post_urls(tag_html: str, limit: int = 6):
    urls = re.findall(r"https://www\.yasong\.net/(\d+)\.html", tag_html)
    seen, out = set(), []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        out.append(f"https://www.yasong.net/{u}.html")
        if len(out) >= limit:
            break
    return out


def process(preset_id: str, slug: str) -> bool:
    if preset_id in SEARCH:
        name = urllib.parse.unquote(slug)
        tag_url = "https://www.yasong.net/?s=" + urllib.parse.quote(name)
        print(f"\n=== {preset_id} (search={name}) ===")
    else:
        name = urllib.parse.unquote(slug)
        tag_url = f"https://www.yasong.net/tag/{slug}/"
        print(f"\n=== {preset_id} (tag={name}) ===")
    tag_html = http_get(tag_url)
    if not tag_html:
        print("  页获取失败")
        return False
    posts = find_post_urls(tag_html)
    print(f"  帖子候选 {len(posts)} 个")
    chosen = None
    for post_url in posts:
        ph = http_get(post_url)
        if not ph:
            continue
        aurl = find_audio_url(ph)
        if not aurl:
            continue
        fname = urllib.parse.unquote(aurl.split("/")[-1])
        if "配乐" in fname:
            print(f"  跳过配乐: {fname[:40]}...")
            continue
        # —— 署名校验：搜索路由噪声大，必须标题含本人姓名，排除合诵/他人主诵 ——
        title = post_title(ph)
        if name not in title:
            continue
        if "合诵" in title or "合" in title:
            print(f"  跳过合诵: {title[:30]}")
            continue
        chosen = (post_url, ph, aurl, fname, title)
        break
    if not chosen:
        print("  未找到署名为本人的纯人声片段")
        return False
    post_url, ph, aurl, fname, title = chosen
    # 下载
    raw = http_get(aurl, binary=True, referer=post_url)
    if not raw or len(raw) < 5000:
        print(f"  下载失败/过小: {fname[:40]}")
        return False
    ext = ".m4a" if aurl.endswith(".m4a") else ".mp3"
    src = DATA / f"_ys_{preset_id}_src{ext}"
    src.write_bytes(raw)
    # 转 wav
    wav = DATA / f"_ys_{preset_id}.wav"
    subprocess.run([FFMPEG, "-y", "-i", str(src), "-ar", "24000", "-ac", "1",
                    str(wav)], capture_output=True, text=True)
    if not wav.exists():
        print(f"  转码失败: {fname[:40]}")
        return False
    dur = get_duration(wav)
    # 起始偏移：长音频可能含站点旁白小传，加深跳过；短视频直接从头附近
    if dur > 120:
        start = 8.0
    elif dur > 30:
        start = 3.0
    else:
        start = 1.0
    start = min(start, max(0.0, dur - TRIM_DUR - 0.5))
    seg = DATA / f"ref_{preset_id}.wav"
    subprocess.run([FFMPEG, "-y", "-ss", f"{start:.2f}", "-t", f"{TRIM_DUR}",
                    "-i", str(wav), "-ar", "24000", "-ac", "1", str(seg)],
                   capture_output=True, text=True)
    # prompt_text：优先用正文朗诵词；无正文(纯播放器页)退回诗名
    if clean_cjk_len(extract_text(ph)) >= 20:
        txt = pick_prompt(extract_text(ph))
    else:
        txt = poem_title_from(title, name)
    (DATA / f"ref_{preset_id}.txt").write_text(txt, encoding="utf-8")
    # 清理中间文件
    src.unlink(missing_ok=True)
    wav.unlink(missing_ok=True)
    print(f"  OK 原长={dur:.0f}s 段长={get_duration(seg):.1f}s 诗名='{txt}' 来源={post_url}")
    return True


def main():
    results = {}
    for pid, slug in TARGETS.items():
        ok = process(pid, slug)
        results[pid] = ok
    print("\n=== 汇总 ===")
    for pid, ok in results.items():
        print(f"  {pid}: {'OK' if ok else 'FAIL'}")
    fails = [p for p, o in results.items() if not o]
    if fails:
        print("失败(需手动处理):", fails)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
