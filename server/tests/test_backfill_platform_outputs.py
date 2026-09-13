"""backfill_platform_outputs 存量升级脚本守护测试（change: multi-platform-final-videos-platform-outputs）。

- _is_legacy_multi_platform / select_legacy_tasks 正确筛出应升级的存量多平台任务；
- enqueue_one 以正确参数入队 subtitle 重跑（clear_outputs + source=backfill）。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database import Base
from app.models.task import Task


@pytest_asyncio.fixture()
async def env():
    """内存 SQLite，仅建 tasks 表。"""
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _seed(session, rows: list[dict]):
    for r in rows:
        session.add(Task(**r))
    import asyncio
    # 同步提交由调用方 await；此处仅构造


async def _make_tasks(session):
    session.add(Task(
        id=1, poem_id=1, platform="douyin,bilibili", platforms=None,
        platform_outputs=None, storyboard='[{"text": "x"}]', status="done",
    ))
    # 已升级（entry 对象）
    session.add(Task(
        id=2, poem_id=1, platform="douyin", platforms='["douyin","bilibili"]',
        platform_outputs=json.dumps({
            "douyin": {"url": "u1", "ratio": "9:16", "status": "ok"},
            "bilibili": {"url": "u2", "ratio": "16:9", "status": "ok"},
        }, ensure_ascii=False),
        storyboard='[{"text": "x"}]', status="done",
    ))
    # 单平台 legacy（不升级）
    session.add(Task(
        id=3, poem_id=1, platform="douyin", platforms=None,
        platform_outputs=None, storyboard='[{"text": "x"}]', status="done",
    ))
    # 旧弱契约多平台（全字符串值，未升级）→ 应升级
    session.add(Task(
        id=4, poem_id=1, platform="douyin,kuaishou", platforms=None,
        platform_outputs=json.dumps({"douyin": "u1", "kuaishou": "u2"}, ensure_ascii=False),
        storyboard='[{"text": "x"}]', status="done",
    ))
    # 多平台但无 storyboard（不可重跑）→ 不升级
    session.add(Task(
        id=5, poem_id=1, platform="douyin,bilibili", platforms=None,
        platform_outputs=None, storyboard=None, status="done",
    ))
    await session.commit()


@pytest.mark.asyncio()
async def test_select_legacy_multi_platform_tasks(env):
    """筛选：t1（空+逗号串）、t4（旧弱契约）入选；t2（已升级）、t3（单平台）、t5（无 storyboard）排除。"""
    from scripts.backfill_platform_outputs import select_legacy_tasks
    async with env() as s:
        await _make_tasks(s)
        ids = await select_legacy_tasks(session=s)
    assert ids == [1, 4], f"期望 [1,4]，实际 {ids}"


@pytest.mark.asyncio()
async def test_enqueue_one_calls_queue_with_subtitle_backfill(env, monkeypatch):
    """enqueue_one 以 stages=['subtitle'], clear_outputs=True, source='backfill' 入队。"""
    from scripts import backfill_platform_outputs as mod

    captured = {}

    class _FakeService:
        async def enqueue_task(self, task_id, stages, clear_outputs, source):
            captured["task_id"] = task_id
            captured["stages"] = stages
            captured["clear_outputs"] = clear_outputs
            captured["source"] = source
            return 1

    monkeypatch.setattr("app.services.queue.queue_service", _FakeService())

    async with env() as s:
        await _make_tasks(s)
        n = await mod.enqueue_one(1)

    assert n == 1
    assert captured["task_id"] == 1
    assert captured["stages"] == ["subtitle"]
    assert captured["clear_outputs"] is True
    assert captured["source"] == "backfill"
