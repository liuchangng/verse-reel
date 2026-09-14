"""课标篇目批量建任务脚本。

读 docs/curriculum_primary.json（已匹配好 poem_id 的小学 75 首），
逐个调后端 POST /api/tasks 创建任务（platforms=[douyin, bilibili]，S 档，双画幅）。

设计：
- 断点续跑：靠 tasks 表的 source_hotspot_title 标记（"curriculum:primary:第N首"），
  已建的跳过，不重复建。
- 不主动发布：用户人工发；脚本只建任务 + 进生成队列。
- 分批控制：默认每批 10 个任务，批间 sleep 避免流水线爆满（视频 1次/分钟限速）。

修复记录（2026-09-14）：``platforms`` 必须以 list 传参。旧版 ``",".join(PLATFORMS)``
被后端解析成单元素 ``["douyin,bilibili"]``，落库畸形并污染产物文件名为
``seg_douyin,bilibili_*.mp4`` 等。详见 ``build_create_params`` 的根因注释。

用法（先起好后端，APP_TOKEN 与 .env 一致）：
  python scripts/batch_curriculum.py [--batch 10] [--dry-run] [--resume]

  --dry-run   只打印要建的任务，不实际调 API
  --resume    跳过 source_hotspot_title 已存在的篇目（默认就跳过，--resume 显式声明）
  --style X   指定文案风格（情感治愈/职场共鸣/历史解读/人生感悟）；
              缺省 = 自动：课标篇目默认「历史解读」，可被本参数覆盖
"""
import argparse
import asyncio
import json
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CURRICULUM = PROJECT_ROOT / "docs" / "curriculum_primary.json"
BASE_URL = "http://127.0.0.1:8000"
# 创建任务 platforms：抖音(9:16 竖屏) + B站(16:9 横屏)，含 S 平台 → 收敛 S 档（小学 75 首）
PLATFORMS = ["douyin", "bilibili"]


def source_tag(no: int, title: str) -> str:
    """断点续跑标记：用 title + 序号唯一，已建任务可识别。"""
    return f"curriculum:primary:{no}"


def build_create_params(item: dict, style: str = "") -> dict:
    """构造 ``POST /api/tasks`` 的查询参数（单一事实源 + 防逗号串回归守护）。

    根因记录（2026-09-14，change-id=curriculum-platforms-param）
    -----------------------------------------------------------
    旧版此处用 ``platforms=",".join(PLATFORMS)`` 发送。后端签名是
    ``platforms: list[str] = Query(None)``，收到单个 ``"douyin,bilibili"`` 时
    解析为**单元素列表** ``["douyin,bilibili"]``——整串被当成「一个平台名」。
    随后 ``create_task`` 落库：``platform='douyin,bilibili'``、
    ``platforms='["douyin,bilibili"]'``（畸形）。

    渲染阶段把 platform 直接拼进文件名，于是产出
    ``seg_douyin,bilibili_0.mp4`` / ``base_douyin,bilibili.mp4`` /
    ``final_douyin,bilibili.mp4``。批量建的 75 个课标任务全部中招
    （守护测试见 ``server/tests/test_curriculum_batch_params.py``）。

    正确做法：传 **list**，由 httpx 序列化为重复参数
    （``platforms=douyin&platforms=bilibili``），FastAPI 方能解析为真实列表。
    """
    # 守护：平台名出现逗号即等价于「串里混了多平台」，必须 fail-loud 而非静默落库
    bad = [p for p in PLATFORMS if not p or p.strip() != p or "," in p]
    if bad:
        raise ValueError(f"平台名非法（不得为空/含逗号/含首尾空白）: {bad!r}")

    params = {
        "poem_id": item["poem_id"],
        # 主平台：仍显式传单平台名，保持日志/兼容语义可读。
        # 注意 create_task 内部会用 platforms[0] 覆盖该值（douyin 即首元素，结果一致）。
        "platform": PLATFORMS[0],
        # 必须传 list，禁止 ",".join —— 见上方根因记录
        "platforms": list(PLATFORMS),
        "source_hotspot_title": source_tag(item["no"], item["title"]),
    }
    # 显式风格优先（--style）；不传则由后端按 curriculum 默认「历史解读」
    if style:
        params["style"] = style
    return params


async def run(args: argparse.Namespace) -> None:
    data = json.loads(CURRICULUM.read_text(encoding="utf-8"))
    items = [it for it in data["items"] if it.get("matched") and it.get("poem_id")]
    print(f"待建任务 {len(items)} 首（已匹配且有效）；批次 {args.batch}；dry_run={args.dry_run}")

    headers = {}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"

    token_file = PROJECT_ROOT / "server" / ".env"
    if not args.token and token_file.exists():
        for line in token_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("APP_TOKEN="):
                headers["Authorization"] = f"Bearer {line.split('=',1)[1].strip()}"
                break

    created = 0
    skipped = 0
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        # 拉现有任务，建 source_tag → task_id 映射（断点续跑）
        existing_tags = set()
        if not args.dry_run:
            r = await client.get("/api/tasks/", params={"page": 1, "page_size": 1000}, headers=headers)
            for t in r.json().get("items", []):
                tag = t.get("source_hotspot_title") or ""
                if tag.startswith("curriculum:primary:"):
                    existing_tags.add(tag)

        for i, it in enumerate(items, 1):
            tag = source_tag(it["no"], it["title"])
            if tag in existing_tags:
                skipped += 1
                print(f"  [skip] #{it['no']} {it['title']} ({tag} 已建)")
                continue
            if args.dry_run:
                print(f"  [dry]  #{it['no']} {it['title']} → poem_id={it['poem_id']} platforms={PLATFORMS}")
                continue
            params = build_create_params(it, args.style)
            r = await client.post("/api/tasks/", params=params, headers=headers)
            if r.status_code == 200:
                created += 1
                print(f"  [ok]   #{it['no']} {it['title']} → task_id={r.json().get('id')}")
            else:
                print(f"  [err]  #{it['no']} {it['title']} → HTTP {r.status_code} {r.text[:120]}")
            # 批控：每 args.batch 个 sleep，避免队列爆满
            if created and created % args.batch == 0 and i < len(items):
                wait = args.batch_interval
                print(f"  ... 已建 {created}，sleep {wait}s 再续 ...")
                await asyncio.sleep(wait)
    print(f"\n=== 完成：新建 {created}，跳过 {skipped}（dry_run={args.dry_run}）===")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="课标篇目批量建任务")
    ap.add_argument("--batch", type=int, default=10, help="每批任务数（默认 10）")
    ap.add_argument("--batch-interval", type=int, default=60, help="批间等待秒（默认 60s，视频限速 1次/分钟）")
    ap.add_argument("--dry-run", action="store_true", help="只打印不建任务")
    ap.add_argument("--resume", action="store_true", help="显式断点续跑（默认即跳过已建）")
    ap.add_argument("--token", default="", help="APP_TOKEN；缺省读 server/.env 的 APP_TOKEN")
    ap.add_argument("--style", default="", help="文案风格（情感治愈/职场共鸣/历史解读/人生感悟）；缺省=自动（课标默认历史解读）")
    args = ap.parse_args()
    asyncio.run(run(args))
