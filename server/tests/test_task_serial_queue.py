"""任务间串行调度 + 前置永久失败级联清障 守护测试（2026-09-09 用户定夺）

背景：
- 任务间串行：资源有限（RPM 限流/视频 1 次/分钟），跨任务并行只会互相抢配额、
  日志穿插没法看。一次只推进一个任务，完成或失败后再轮到下一个。
- 级联清障：前置阶段永久失败后，依赖它的 pending Job 曾永远挂着僵尸，
  轮询器每 2s 打一条"不自动补建"警告无限刷屏。现改为级联失败全部 pending Job，
  任务干净落 failed 终态。

测试用独立内存库（aiosqlite），monkeypatch 掉 queue 模块里的 session 工厂与
_run_job，不触碰真实数据库、不真正执行流水线。
"""
import asyncio

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database import Base
import app.services.queue as qmod
from app.models.job import Job
from app.models.task import Task
from app.services.queue import QueueService


@pytest_asyncio.fixture()
async def queue_env(monkeypatch):
    """独立内存库 + 已 patch 会话工厂的 QueueService（_run_job 打成 no-op）。"""
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(qmod, "async_session_factory", factory)

    svc = QueueService()

    async def _noop_run_job(job_id, sem):
        return None

    monkeypatch.setattr(svc, "_run_job", _noop_run_job)
    yield svc, factory
    await engine.dispose()


async def _mk_task(factory, task_id: int) -> None:
    async with factory() as s:
        s.add(Task(id=task_id, poem_id=1, platform="douyin"))
        await s.commit()


async def _mk_job(factory, task_id: int, stage: str, status: str = "pending") -> int:
    from app.services.queue import STAGE_PRIORITY
    async with factory() as s:
        job = Job(task_id=task_id, stage=stage, status=status,
                  priority=STAGE_PRIORITY.get(stage, 0))
        s.add(job)
        await s.commit()
        return job.id


@pytest.mark.asyncio()
async def test_tasks_run_serially(queue_env):
    """两个任务都有待跑 Job 时，一次认领只推进先入队任务的 Job，不跨任务并行。"""
    svc, factory = queue_env
    await _mk_task(factory, 1)
    await _mk_task(factory, 2)
    await _mk_job(factory, 1, "script")   # task1 先入队
    await _mk_job(factory, 2, "script")

    claimed = await svc._claim_and_dispatch()
    assert claimed == 1

    # 第二次扫描：task1 的 script 已 running（no-op 不结束），task2 不得被认领
    claimed = await svc._claim_and_dispatch()
    assert claimed == 0, "任务 1 未完成时，任务 2 的 Job 不应被认领（任务间串行）"

    async with factory() as s:
        statuses = (await s.execute(
            qmod.select(Job).order_by(Job.id)
        )).scalars().all()
    by_task = {j.task_id: j.status for j in statuses}
    assert by_task[1] == "running"
    assert by_task[2] == "pending", "任务 2 必须等任务 1 结束后才开始"


@pytest.mark.asyncio()
async def test_serial_advances_after_task_done(queue_env):
    """先入队任务的 Job 全部结束后，下一个任务才开始推进。"""
    svc, factory = queue_env
    await _mk_task(factory, 1)
    await _mk_task(factory, 2)
    await _mk_job(factory, 1, "script")
    await _mk_job(factory, 2, "script")

    assert await svc._claim_and_dispatch() == 1

    # 手动结束 task1 的 running Job（模拟 _run_job 完成）
    async with factory() as s:
        job = (await s.execute(qmod.select(Job).where(Job.task_id == 1))).scalars().one()
        job.status = "done"
        await s.commit()

    assert await svc._claim_and_dispatch() == 1, "任务 1 完成后应立即轮到任务 2"
    async with factory() as s:
        job2 = (await s.execute(qmod.select(Job).where(Job.task_id == 2))).scalars().one()
    assert job2.status == "running"


@pytest.mark.asyncio()
async def test_cascade_fail_when_prereq_permanently_failed(queue_env):
    """前置永久失败 → 依赖它的 pending Job 级联失败，任务落 failed 终态，不留僵尸。"""
    svc, factory = queue_env
    await _mk_task(factory, 3)
    # script 永久失败（重试耗尽），character/subtitle 等 pending
    j_script = await _mk_job(factory, 3, "script", status="failed")
    await _mk_job(factory, 3, "character")
    await _mk_job(factory, 3, "subtitle")

    claimed = await svc._claim_and_dispatch()
    # character 依赖 script（产物为空）→ _heal_prereqs 触发级联；不派发任何 Job
    assert claimed == 0

    async with factory() as s:
        jobs = (await s.execute(
            qmod.select(Job).where(Job.task_id == 3)
        )).scalars().all()
        task = await s.get(Task, 3)
    by_stage = {j.stage: j.status for j in jobs}
    assert by_stage["character"] == "failed", "依赖失败前置的 pending Job 应级联失败"
    assert by_stage["subtitle"] == "failed", "所有 pending Job 都应清掉，不留僵尸"
    assert by_stage["script"] == "failed"
    assert task.status == "failed", "任务应干净落 failed 终态"

    # 幂等：再扫一遍不应再打级联日志（无 pending 可清），也不派发
    claimed = await svc._claim_and_dispatch()
    assert claimed == 0


@pytest.mark.asyncio()
async def test_running_job_blocks_other_tasks(queue_env):
    """有 running Job（属于 task1）时，即使 task2 的 Job 依赖满足也不认领。"""
    svc, factory = queue_env
    await _mk_task(factory, 1)
    await _mk_task(factory, 2)
    await _mk_job(factory, 1, "tts", status="running")   # task1 慢阶段在跑
    await _mk_job(factory, 2, "script")

    assert await svc._claim_and_dispatch() == 0, "在跑任务存在时不得认领其他任务的 Job"
