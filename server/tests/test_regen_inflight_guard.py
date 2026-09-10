"""重新生成「在途互斥」守护测试（2026-09-09 任务001 事故）

事故：任务 1 的分镜图在用户没有任何操作的情况下被重跑了一遍（Job#42 image
跑了 46 秒），用户质问"我没点重新生成，为啥又重新生成了"。根因链路：
重跑视频 → ``_expand_prereqs(['video'])`` 补全出 [image, video] 两条 Job →
``clear_outputs`` 清空 image_urls → image 阶段产物自检不通过 → 整组分镜图重生成。
即：**一次重生成请求会静默丢弃在途进度并清空已生成产物**。

修复（两条硬规则）：
1. 审计：``enqueue_task`` 必须自报 source，覆盖在途 Job 时打 WARNING（含被覆盖明细）
2. 互斥：任务仍有 pending/running Job 时，POST /regenerate 默认 409；
   前端二次确认后带 ``force=true`` 才放行。
"""
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.api.tasks import regenerate_task
from app.database import Base
from app.models.job import Job
from app.models.task import Task
import app.api.tasks as tasks_api


@pytest_asyncio.fixture()
async def db_factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    calls = []

    async def fake_enqueue(task_id, stages=None, clear_outputs=False, source="unknown"):
        calls.append({
            "task_id": task_id,
            "stages": stages,
            "clear_outputs": clear_outputs,
            "source": source,
        })
        return len(stages) if stages else 6

    monkeypatch.setattr(tasks_api.queue_service, "enqueue_task", fake_enqueue)
    yield factory, calls
    await engine.dispose()


async def _mk_task(factory, status="processing", stage="image"):
    async with factory() as db:
        t = Task(poem_id=1, status=status, current_stage=stage, platform="douyin")
        db.add(t)
        await db.commit()
        return t.id


@pytest.mark.asyncio()
async def test_regenerate_blocked_when_inflight(db_factory):
    """有 pending/running Job 时必须 409，且不得入队（防静默覆盖）。"""
    factory, calls = db_factory
    tid = await _mk_task(factory)
    async with factory() as db:
        db.add(Job(task_id=tid, stage="image", status="running", priority=50))
        await db.commit()

    with pytest.raises(HTTPException) as ei:
        await regenerate_task(task_id=tid, stage="video", force=False, db=factory())
    assert ei.value.status_code == 409
    assert "正在执行中" in ei.value.detail
    assert calls == [], "被 409 拦截时不得入队，否则产物已被清空"


@pytest.mark.asyncio()
async def test_regenerate_force_overrides_inflight(db_factory):
    """force=true 才允许覆盖在途 Job（用户已二次确认）。"""
    factory, calls = db_factory
    tid = await _mk_task(factory)
    async with factory() as db:
        db.add(Job(task_id=tid, stage="image", status="pending", priority=50))
        await db.commit()

    # 注意：force 分支直接调 enqueue_task（monkeypatch 掉），不会真的删 Job
    await regenerate_task(task_id=tid, stage="video", force=True, db=factory())
    assert len(calls) == 1
    assert calls[0]["source"] == "regenerate", "入队必须自报来源以便审计"
    assert calls[0]["clear_outputs"] is True


@pytest.mark.asyncio()
async def test_regenerate_allowed_when_no_inflight(db_factory):
    """无在途 Job（failed / pending_review 等终态）时正常放行，不打扰用户。"""
    factory, calls = db_factory
    tid = await _mk_task(factory, status="failed", stage="video")
    async with factory() as db:
        db.add(Job(task_id=tid, stage="video", status="failed", priority=10))
        db.add(Job(task_id=tid, stage="tts", status="done", priority=45))
        await db.commit()

    resp = await regenerate_task(task_id=tid, stage="video", force=False, db=factory())
    assert resp["task_id"] == tid
    assert len(calls) == 1
    assert calls[0]["stages"] == ["video"]


@pytest.mark.asyncio()
async def test_regenerate_sets_task_processing(db_factory):
    """放行后任务立即翻 processing 并清空错误，前端不必等队列 claim。"""
    factory, _ = db_factory
    tid = await _mk_task(factory, status="failed", stage="video")
    await regenerate_task(task_id=tid, stage="video", force=False, db=factory())
    async with factory() as db:
        t = (await db.execute(select(Task).where(Task.id == tid))).scalar_one()
        assert t.status == "processing"
        assert t.error_message is None
        assert t.current_stage == "video"
