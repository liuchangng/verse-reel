"""资源分离队列（A2）+ 级联清障 守护测试

背景（2026-09-12 A2 架构，用户定夺）：
- 旧版"严格任务间串行"把并发锁在单个任务上，75 首一条条跑太慢。A2 改为
  **按资源类分桶并发**：text/image/tts/video/local 各自独立闸门，跨任务并行；
  同一资源类的所有阶段共享一个桶（如 character/image 同抢图片并发）。
- video 仍 video_concurrency=1 + 62s 最小间隔（agnes 1 次/分钟），且按 task_id
  升序派发 → 交付顺序 = 任务创建顺序（A2 视频门 FIFO）。
- 级联清障：前置阶段永久失败后，依赖它的 pending Job 曾永远挂着僵尸，轮询器每
  2s 打一条警告无限刷屏。现改为级联失败全部 pending Job，任务干净落 failed 终态。

测试用独立内存库（aiosqlite），monkeypatch 掉 queue 模块里的 session 工厂与
_run_job，不触碰真实数据库、不真正执行流水线。fixture 的 _run_job no-op **不释放
信号量** —— 模拟"Job 仍在跑"（未收工），从而可断言资源桶的并发上限。
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
    # 固定各方并发=1，让断言与外部（可能被其他用例改过的全局 settings）解耦
    for attr in ("text_concurrency", "image_concurrency", "tts_concurrency",
                 "subtitle_concurrency", "video_concurrency"):
        monkeypatch.setattr(qmod.settings, attr, 1)

    svc = QueueService()

    async def _noop_run_job(job_id, sem):
        # 不释放信号量：模拟"Job 已认领、仍在执行中"
        return None

    monkeypatch.setattr(svc, "_run_job", _noop_run_job)
    yield svc, factory
    await engine.dispose()


async def _mk_task(factory, task_id: int, **fields) -> None:
    async with factory() as s:
        s.add(Task(id=task_id, poem_id=1, platform="douyin", **fields))
        await s.commit()


async def _mk_job(factory, task_id: int, stage: str, status: str = "pending") -> int:
    from datetime import datetime, timezone
    from app.services.queue import STAGE_PRIORITY
    async with factory() as s:
        job = Job(task_id=task_id, stage=stage, status=status,
                  priority=STAGE_PRIORITY.get(stage, 0),
                  queued_at=datetime.now(timezone.utc))
        s.add(job)
        await s.commit()
        return job.id


def _release(svc: QueueService, stage: str) -> None:
    """手动释放某阶段的资源桶（模拟其 _run_job 收工）。"""
    svc._sems[qmod._resource_class(stage)].release()


# ---------------------------------------------------------------- #
# A2：按资源类分桶并发
# ---------------------------------------------------------------- #

@pytest.mark.asyncio()
async def test_same_resource_serial_across_tasks(queue_env):
    """同一资源类（text）跨任务共享一个桶：text 并发=1 时，task2 的 script
    必须等 task1 的 script 收工（桶满），不再受"任务间串行"全局锁控制。"""
    svc, factory = queue_env
    await _mk_task(factory, 1)
    await _mk_task(factory, 2)
    await _mk_job(factory, 1, "script")
    await _mk_job(factory, 2, "script")

    assert await svc._claim_and_dispatch() == 1, "首轮认领 task1 的 script"

    # task1 仍在跑（no-op 不释放），text 桶已满 → task2 不得被认领
    assert await svc._claim_and_dispatch() == 0, "text 桶满时 task2 的 script 不应被认领"

    async with factory() as s:
        by_task = {j.task_id: j.status for j in (await s.execute(qmod.select(Job))).scalars().all()}
    assert by_task[1] == "running"
    assert by_task[2] == "pending"


@pytest.mark.asyncio()
async def test_cross_resource_parallel_across_tasks(queue_env):
    """A2 核心：不同资源类跨任务并行 —— task1 的 script（text 桶）与 task2 的
    image（image 桶）可同轮同时认领（旧版会被"任务间串行"锁死）。"""
    svc, factory = queue_env
    await _mk_task(factory, 1)
    # task2 前置已产出（script + 定妆照），使 image 依赖满足
    await _mk_task(factory, 2, script="文案", character_ref="https://x/ref.png")
    await _mk_job(factory, 1, "script")
    await _mk_job(factory, 2, "image")

    claimed = await svc._claim_and_dispatch()
    assert claimed == 2, "不同资源类应跨任务并行认领（text + image）"

    async with factory() as s:
        by_stage = {j.stage: j.status for j in (await s.execute(qmod.select(Job))).scalars().all()}
    assert by_stage["script"] == "running"
    assert by_stage["image"] == "running"


@pytest.mark.asyncio()
async def test_video_door_fifo_by_task_id(queue_env):
    """A2 视频门：video 并发=1 且按 task_id 升序派发 → 交付顺序 = 任务创建顺序。"""
    svc, factory = queue_env
    for tid in (1, 2, 3):
        # image 产物存在 → video 依赖满足
        await _mk_task(factory, tid, image_urls='["https://x/sb.png"]')
        await _mk_job(factory, tid, "video")

    assert await svc._claim_and_dispatch() == 1, "video 桶=1，首轮只认领 1 个"

    async with factory() as s:
        rows = (await s.execute(qmod.select(Job).where(Job.status == "running"))).scalars().all()
    assert [j.task_id for j in rows] == [1], "必须先派 task_id 最小的 video（FIFO）"

    # task1 video 收工 → 释放 video 桶，下一轮轮到 task2
    _release(svc, "video")
    async with factory() as s:
        j = (await s.execute(qmod.select(Job).where(Job.task_id == 1))).scalars().one()
        j.status = "done"
        await s.commit()

    assert await svc._claim_and_dispatch() == 1
    async with factory() as s:
        rows = (await s.execute(qmod.select(Job).where(Job.status == "running"))).scalars().all()
    assert [j.task_id for j in rows] == [2], "task1 收工后应轮到 task2 的 video"


@pytest.mark.asyncio()
async def test_publish_copy_stage_wired(queue_env):
    """publish_copy 阶段：仅依赖 script、归 text 资源类、在标准阶段序列内。"""
    assert "publish_copy" in qmod.STAGE_ORDER
    assert qmod.STAGE_PREREQS["publish_copy"] == {"script"}
    assert qmod._resource_class("publish_copy") == "text"


# ---------------------------------------------------------------- #
# 级联清障 / 依赖阻断
# ---------------------------------------------------------------- #

@pytest.mark.asyncio()
async def test_cascade_fail_when_prereq_permanently_failed(queue_env):
    """前置永久失败 → 依赖它的 pending Job 级联失败，任务落 failed 终态，不留僵尸。"""
    svc, factory = queue_env
    await _mk_task(factory, 3)
    # script 永久失败（重试耗尽），character/subtitle 等 pending
    await _mk_job(factory, 3, "script", status="failed")
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
    assert await svc._claim_and_dispatch() == 0


@pytest.mark.asyncio()
async def test_prereq_running_blocks_downstream_even_with_product(queue_env):
    """前置 Job 仍在跑（哪怕产物已中途落库）时，下游 Job 不得被认领。

    真实事故：_generate_script 中途先提交 task.script，tts 据此通过依赖
    检查抢跑，读到空 storyboard 三连失败并级联拖死 image/video/subtitle。
    """
    svc, factory = queue_env
    await _mk_task(factory, 1)
    # script 产物已"存在"（模拟 _generate_script 的中途提交），但其 Job 在途
    async with factory() as s:
        t = await s.get(Task, 1)
        t.script = "文案正文「金句」。"
        await s.commit()
    await _mk_job(factory, 1, "script", status="running")
    await _mk_job(factory, 1, "tts")

    assert await svc._claim_and_dispatch() == 0, "前置 Job 在途时，下游 Job 不得被认领"

    # script Job 收工 → tts 立即可认领（不误伤正常推进）
    async with factory() as s:
        from sqlalchemy import select as _select
        job = (await s.execute(_select(Job).where(Job.stage == "script"))).scalar_one()
        job.status = "done"
        await s.commit()
    assert await svc._claim_and_dispatch() == 1, "前置收工后，下游 Job 必须放行"


# ---------------------------------------------------------------- #
# 孤儿清理 / 启动自愈
# ---------------------------------------------------------------- #

@pytest.mark.asyncio()
async def test_orphan_pending_jobs_cleaned_not_starving(queue_env):
    """孤儿防御：已删除任务的 pending Job 被清理，队列同轮继续推进其他任务。"""
    svc, factory = queue_env
    await _mk_task(factory, 1)
    # task1 已有分镜图 → video 的前置（image）视为已满足
    async with factory() as s:
        t = await s.get(Task, 1)
        t.image_urls = '["https://example.com/sb-1.png"]'
        await s.commit()
    # task2 不存在（已被删除），但其 pending Job 还在（存量脏数据）
    orphan_id = await _mk_job(factory, 2, "character")   # priority 55，排 task1 前面
    await _mk_job(factory, 1, "video")                    # priority 10

    # 清理孤儿后，同轮应正常认领 task1 的 video（不再被孤儿饿死）
    assert await svc._claim_and_dispatch() == 1
    async with factory() as s:
        assert await s.get(Job, orphan_id) is None, "孤儿 pending Job 应被清理"
        v = (await s.execute(qmod.select(Job).where(Job.task_id == 1))).scalars().one()
    assert v.status == "running", "清理孤儿后，其他任务必须能正常推进"


@pytest.mark.asyncio()
async def test_recover_enqueues_jobless_processing_tasks(queue_env):
    """启动自愈：旧直跑路径遗留的 processing 任务没有任何 Job，
    重启后 recover() 应补建全部**启用**阶段 Job（video 默认关闭故不建）。"""
    svc, factory = queue_env
    async with factory() as s:
        s.add(Task(id=9, poem_id=1, platform="douyin", status="processing"))
        await s.commit()

    await svc.recover()

    enabled = set(qmod._enabled_stages())
    async with factory() as s:
        jobs = (await s.execute(
            qmod.select(Job).where(Job.task_id == 9)
        )).scalars().all()
    assert {j.stage for j in jobs} == enabled, "应补建全部启用阶段"
    assert all(j.status == "pending" for j in jobs)

    # 幂等：再次 recover 不重复补建
    await svc.recover()
    async with factory() as s:
        jobs2 = (await s.execute(
            qmod.select(Job).where(Job.task_id == 9)
        )).scalars().all()
    assert len(jobs2) == len(enabled)
