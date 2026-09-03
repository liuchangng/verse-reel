"""从 elevenlabs 古风曲预览页(_el.html)解析情绪并下载真实免版权 BGM。

- 解析页面中的 payloadcms mp3 直链，按上下文情绪词映射到 mood（calm/sad/epic/nostalgic/general）。
- 下载到 server/assets/bgm/<mood>_<n>.mp3；已存在则跳过（支持用户放入真实曲目覆盖）。
- 真实曲优先于 scripts/gen_bgm.py 的合成兜底。

用法：先 curl 页面到 _el.html，再 python scripts/fetch_bgm.py
"""
import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.abspath(os.path.join(BASE, "..", "server", "assets", "bgm"))
os.makedirs(OUT, exist_ok=True)

html_path = "_el.html"
if not os.path.exists(html_path):
    print("缺少 _el.html，请先: curl -sL <elevenlabs chinese 页面> -o _el.html")
    sys.exit(1)
html = open(html_path, encoding="utf-8", errors="ignore").read()

mp3s = []
seen = set()
for m in re.findall(r"https://eleven-public-cdn\.elevenlabs\.io/payloadcms/[\w]+\.mp3", html):
    if m not in seen:
        seen.add(m)
        mp3s.append(m)

mood_words = {
    "Serene": "calm", "Peaceful": "calm", "Contemplative": "calm",
    "Meditative": "calm", "Tranquil": "calm",
    "Melancholic": "sad", "Emotional": "sad", "Sad": "sad",
    "Epic": "epic", "Adventurous": "epic", "Majestic": "epic",
    "Dramatic": "epic", "Energetic": "epic",
    "Hopeful": "nostalgic", "Reflective": "nostalgic", "Nostalgic": "nostalgic",
}
title_rule = [
    (("garden", "serenade", "whispers", "temple", "bamboo"), "calm"),
    (("empire", "blade", "warrior", "dynasty", "crimson"), "epic"),
    (("reflections", "river", "moon", "journey"), "nostalgic"),
    (("melancholic",), "sad"),
]


def decide(ctx: str, title: str) -> str:
    for w, mk in mood_words.items():
        if re.search(r"\b" + re.escape(w) + r"\b", ctx, re.I):
            return mk
    low = (title + " " + ctx).lower()
    for kws, mk in title_rule:
        if any(k in low for k in kws):
            return mk
    return "general"


counts = {}
for m in mp3s:
    i = html.find(m)
    ctx = html[max(0, i - 900): i + 60]
    tt = re.findall(r"Jade [A-Za-z ]{3,40}", ctx)
    title = tt[-1] if tt else ""
    mk = decide(ctx, title)
    counts[mk] = counts.get(mk, 0) + 1
    n = counts[mk]
    name = f"{mk}_{n}.mp3"
    path = os.path.join(OUT, name)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        print(f"[skip] {name}")
        continue
    print(f"[get] {name} <- {m.split('/')[-1]} ({title})")
    r = subprocess.run(["curl", "-sL", "--max-time", "90", "-o", path, m],
                       capture_output=True, text=True)
    if r.returncode != 0 or os.path.getsize(path) < 1000:
        print(f"  FAIL rc={r.returncode}")
        if os.path.exists(path):
            os.remove(path)

print("done counts:", counts)
