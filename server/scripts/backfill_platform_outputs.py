"""多平台成片存量升级：legacy 任务重跑 subtitle 生成各平台独立成片。

change: multi-platform-final-videos-platform-outputs（REQ-M1 存量升级）

背景
----
本分支把 ``tasks.platform_outputs`` 从弱契约 ``{platform: url}`` 升级为一等 entry 对象
``{platform: {url, ratio, duration?, status, error?}}``；新任务 / 重跑 subtitle 会产出
各平台独立成片（含真实 16:9 横屏）。但**存量旧任务**：

- ``platform_outputs`` 为空（或旧弱契约字符串）；
- 平台以逗号串存于 ``task.platform``（如 ``"douyin,bilibili"``），``task.platforms`` 未填。

这类任务在前端被按名义比例各建一张卡，B站卡标 16:9 实则播 9:16 主片（无独立横屏成片）。
本脚本筛选这些「应升级」的存量多平台任务，重新入队 subtitle 阶段，由 worker 按任务
真实平台重渲染、写入 entry 对象。

入队行为
--------
调用 ``queue_service.enqueue_task(task_id, stages=["subtitle"], clear_outputs=True,
source="backfill")``。该调用会：清掉旧 ``subtitle_url`` / ``platform_outputs``（REQ-M4
队列清理），置任务为 processing，并创建 pending 的 subtitle Job；worker 消费后走
``run_stage("subtitle")`` → ``_render_platform_outputs``（已修 ``_task_platforms`` 逗号串
兜底），产出各平台真实 entry。

筛选条件（_is_legacy_multi_platform）
-------------------------------
- 多平台标志：``task.platform`` 含逗号，或 ``task.platforms`` JSON 解析后 >1；
- 尚未升级：``platform_outputs`` 为空，或为旧弱契约（值全为字符串、无 entry 对象）；
  已是 entry 对象（含 ``status``）的任务视为已升级，跳过；
- 可重跑：``task.storyboard`` 非空（subtitle 阶段前置）。

用法（在 server/ 目录）
-----------------------
    uv run python scripts/backfill_platform_outputs.py --dry-run     # 只列出待升级任务
    uv run python scripts/backfill_platform_outputs.py --execute     # 实际入队 subtitle 重跑
    uv run python scripts/backfill_platform_outputs.py --execute --limit 50
"""
import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

# 让脚本可直接 `python scripts/backfill_platform_outputs.py` 导入 app.*
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.models.task import Task

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_platform_outputs")


def _is_legacy_multi_platform(task) -> bool:
    """判断是否为应升级的存量多平台任务（纯字段判断，不触库）。"""
    # 1) 多平台标志：platform 逗号串 或 platforms JSON 解析 >1
    plats: list[str] = []
    legacy = (task.platform or "").strip()
    if "," in legacy:
        plats = [p.strip() for p in legacy.split(",") if p.strip()]
    if not plats and task.platforms:
        try:
            plats = json.loads(task.platforms) or []
        except (TypeError, ValueError):
            plats = []
    if len(plats) < 2:
        return False

    # 2) 已升级判据：platform_outputs 中存在 entry 对象（含 status 字段）→ 跳过
    if task.platform_outputs:
        try:
            data = json.loads(task.platform_outputs)
        except (TypeError, ValueError):
            data = None
        if isinstance(data, dict) and data:
            has_entry_obj = any(
                isinstance(v, dict) and "status" in v for v in data.values()
            )
            if has_entry_obj:
                return False
            # 全是字符串值（旧弱契约）→ 视为未升级，继续升级

    # 3) 可重跑：需有 storyboard（subtitle 阶段前置）
    if not task.storyboard:
        return False

    return True


async def _select_legacy_ids(session) -> list[int]:
    """从给定 session 选出待升级任务 id（按 id 升序）。可单测。"""
    stmt = select(
        Task.id, Task.platform, Task.platforms,
        Task.platform_outputs, Task.storyboard,
    )
    rows = (await session.execute(stmt)).all()
    ids: list[int] = []
    for tid, platform, platforms, po, storyboard in rows:
        t = SimpleNamespace(
            id=tid, platform=platform, platforms=platforms,
            platform_outputs=po, storyboard=storyboard,
        )
        if _is_legacy_multi_platform(t):
            ids.append(tid)
    return ids


async def select_legacy_tasks(limit: int | None = None, session=None) -> list[int]:
    """返回待升级任务 id 列表（按 id 升序）。session 省略时用应用默认工厂。"""
    if session is not None:
        ids = await _select_legacy_ids(session)
    else:
        from app.database import async_session_factory
        async with async_session_factory() as s:
            ids = await _select_legacy_ids(s)
    if limit:
        ids = ids[:limit]
    return ids


async def enqueue_one(task_id: int) -> int:
    """入队单个任务的 subtitle 重跑（清旧产物 + 置 processing）。"""
    from app.services.queue import queue_service
    return await queue_service.enqueue_task(
        task_id=task_id,
        stages=["subtitle"],
        clear_outputs=True,
        source="backfill",
    )


async def main() -> int:
    ap = argparse.ArgumentParser(description="多平台成片存量升级：重跑 subtitle 生成各平台独立成片")
    ap.add_argument("--dry-run", action="store_true", help="只列出待升级任务，不入队（默认）")
    ap.add_argument("--execute", action="store_true", help="实际入队 subtitle 重跑")
    ap.add_argument("--limit", type=int, default=None, help="最多处理 N 个任务")
    args = ap.parse_args()

    # 默认 dry-run，避免误改生产数据
    dry_run = not args.execute

    ids = await select_legacy_tasks(limit=args.limit)
    print(f"待升级存量多平台任务: {len(ids)} 个")
    for tid in ids[:50]:
        print(f"  task={tid}")
    if len(ids) > 50:
        print(f"  ... 其余 {len(ids) - 50} 个省略")

    if dry_run:
        print("(dry-run) 未做任何修改。加 --execute 实际入队 subtitle 重跑。")
        return 0

    count = 0
    for tid in ids:
        n = await enqueue_one(tid)
        count += n
        print(f"  enqueued task={tid} jobs={n}")
    print(f"已入队 {len(ids)} 个任务的 subtitle 重跑（共 {count} 个 Job）；"
          f"worker 将按各任务真实平台重渲染并写回 platform_outputs entry 对象。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
