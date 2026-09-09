"""重新生成：缺失前置补建 + 执行日志追加 守护测试（2026-09-09 任务001死循环事故）

事故链：
1. ``_task_produced`` 把 image 判定写成 ``image_urls or character_ref``——
   定妆照在而分镜图空时 image 被误判"已产出"，``_expand_prereqs`` 永不补建
   image，重新生成只入队 video+subtitle → video 三连失败"缺分镜图" →
   subtitle 级联失败 → 再点重新生成进入同一循环，永无出口。
2. ``enqueue_task`` 物理删除全部旧 Job，每次重新生成执行历史清零——
   用户定夺：重新生成应"接着缺失的步骤继续 + 日志是追加的"。

修复语义：
- image 产物只看 image_urls（character 阶段只写 character_ref，不共用）；
- 重新生成只清理 pending/running 旧 Job，done/failed 终态 Job 保留为历史；
- /tasks/{id}/jobs 按阶段聚合：尝试日志按 Job.id 顺序拼接，状态取最新。
"""
import json

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database import Base
import app.services.queue as qmod
from app.api.tasks import get_task_jobs
from app.models.job import Job
from app.models.task import Task
from app.services.queue import (
    QueueService,
    STAGE_ORDER,
    STAGE_PRIORITY,
    _expand_prereqs,
    _task_produced,
)


@pytest_asyncio.fixture()
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(qmod, "async_session_factory", factory)
    yield factory
    await engine.dispose()


async def _mk_task(factory, task_id: int, **fields) -> None:
    async with factory() as s:
        s.add(Task(id=task_id, poem_id=1, platform="douyin", **fields))
        await s.commit()


async def _mk_job(factory, task_id: int, stage: str, status: str = "pending",
                  attempts: int = 0, attempts_log: str | None = None) -> int:
    async with factory() as s:
        job = Job(task_id=task_id, stage=stage, status=status, attempts=attempts,
                  attempts_log=attempts_log, priority=STAGE_PRIORITY.get(stage, 0))
        s.add(job)
        await s.commit()
        return job.id


# ---------------------------------------------------------------- #
# 1. image 产物判定：只看 image_urls，定妆照不算
# ---------------------------------------------------------------- #

def test_task_produced_image_requires_image_urls():
    """定妆照存在、分镜图为空 → image 必须判定为未产出。"""
    task = Task(id=1, poem_id=1, platform="douyin",
                character_ref="http://img/char.png", image_urls=None)
    produced = _task_produced(task)
    assert produced["image"] is False, "分镜图为空时 image 不得视为已产出"
    assert produced["character"] is True


def test_expand_prereqs_backfills_image_for_video():
    """重新生成 video 而分镜图缺失时，入队必须自动补建 image（死循环出口）。"""
    task = Task(id=1, poem_id=1, platform="douyin",
                script="文案", character_ref="http://img/char.png",
                image_urls=None, audio_url="http://a.mp3")
    expanded = _expand_prereqs(["video"], _task_produced(task))
    assert "image" in expanded, "分镜图缺失时必须补建 image 前置"
    assert expanded == [s for s in STAGE_ORDER if s in set(expanded)]


# ---------------------------------------------------------------- #
# 2. 重新生成保留终态 Job 历史，只清 pending/running
# ---------------------------------------------------------------- #

@pytest.mark.asyncio()
async def test_enqueue_preserves_terminal_jobs_as_history(env):
    """重新生成时 done/failed 旧 Job 保留（日志追加），pending/running 清掉。"""
    svc = QueueService()
    # 产物现状：script/character/tts 已有，image/video/subtitle 缺失
    # → 重新生成 ['video','subtitle'] 时 expand 只补 [image, video, subtitle]
    await _mk_task(env, 1, script="文案正文", character_ref="http://img/char.png",
                   audio_url="http://a.mp3")
    failed_id = await _mk_job(env, 1, "video", status="failed", attempts=3,
                              attempts_log=json.dumps([{"attempt": 1, "ok": False}]))
    done_id = await _mk_job(env, 1, "script", status="done", attempts=1)
    stale_id = await _mk_job(env, 1, "subtitle", status="pending")
    async with env() as s:
        from sqlalchemy import update
        await s.execute(update(Job).where(Job.id == stale_id)
                        .values(last_error="stale-marker"))
        await s.commit()

    count = await svc.enqueue_task(task_id=1, stages=["video", "subtitle"],
                                   clear_outputs=False)

    async with env() as s:
        rows = (await s.execute(select(Job).order_by(Job.id.asc()))).scalars().all()
    by_id = {j.id: j for j in rows}
    # 终态历史保留
    assert by_id[failed_id].status == "failed", "failed 旧 Job 必须保留为历史"
    assert by_id[done_id].status == "done", "done 旧 Job 必须保留为历史"
    # pending 旧 Job 清理（按标记识别，规避 SQLite 复用被删行 id）
    assert not any(j.last_error == "stale-marker" for j in rows), \
        "pending 旧 Job 必须清理（否则重复消费）"
    # 新 Job 只为补全后的 stages 创建
    new_jobs = [j for j in rows if j.id not in (failed_id, done_id)]
    assert {j.stage for j in new_jobs} == {"image", "video", "subtitle"}
    assert all(j.status == "pending" for j in new_jobs)
    assert count == 3


# ---------------------------------------------------------------- #
# 3. jobs API 按阶段聚合：历史尝试拼接、状态取最新
# ---------------------------------------------------------------- #

@pytest.mark.asyncio()
async def test_get_task_jobs_merges_history_per_stage(env):
    """同阶段多 Job（历史 failed + 新 done）聚合为一条：日志最新在前、编号连续。"""
    await _mk_task(env, 1)
    old_fail = json.dumps([
        {"attempt": 1, "ok": False, "at": "2026-09-09T21:31:54", "error": "缺分镜图"},
        {"attempt": 2, "ok": False, "at": "2026-09-09T21:31:56", "error": "缺分镜图"},
    ])
    await _mk_job(env, 1, "video", status="failed", attempts=2, attempts_log=old_fail)
    new_log = json.dumps([{"attempt": 1, "ok": True, "at": "2026-09-09T22:00:01"}])
    await _mk_job(env, 1, "video", status="done", attempts=1, attempts_log=new_log)
    await _mk_job(env, 1, "script", status="done", attempts=1,
                  attempts_log=json.dumps([{"attempt": 1, "ok": True}]))

    async with env() as db:
        data = await get_task_jobs(1, db)

    # video 最近活动更晚 → 阶段卡片按最近活动倒序，video 排最前
    assert [j["stage"] for j in data["jobs"]] == ["video", "script"], \
        "阶段卡片按最近活动时间倒序（最新的在最上面）"
    video = data["jobs"][0]
    assert video["status"] == "done", "状态取最新一条 Job"
    assert video["attempts"] == 3, "尝试次数跨轮连续累计"
    assert [a["attempt"] for a in video["attempts_log"]] == [3, 2, 1], \
        "编号跨轮连续，日志最新在前"

    # 扁平执行日志：全局连续编号（跨阶段跨轮），最新在最上面
    logs = data["logs"]
    assert [(a["seq"], a["stage"], a["ok"]) for a in logs] == [
        (4, "script", True), (3, "video", True),
        (2, "video", False), (1, "video", False),
    ], "logs 按时间倒序、全局连续编号、带阶段字段"


# ---------------------------------------------------------------- #
# 4. 任务终态判定：只看每阶段最新 Job，活动 Job 在途不判定
# ---------------------------------------------------------------- #

@pytest.mark.asyncio()
async def test_finalize_skipped_while_jobs_active(env):
    """有 pending Job（重试在途）时不得判终态——旧代码会被历史 failed 拖成 failed。"""
    svc = QueueService()
    await _mk_task(env, 1, status="processing")
    await _mk_job(env, 1, "video", status="failed", attempts=3)   # 旧历史
    await _mk_job(env, 1, "video", status="done")                 # 新一轮成功
    await _mk_job(env, 1, "subtitle", status="pending")           # 新一轮在途

    async with env() as s:
        await svc._maybe_finalize(s, 1)
        t = await s.get(Task, 1)
    assert t.status == "processing", "存在在途 Job 时不得做终态判定"


@pytest.mark.asyncio()
async def test_finalize_latest_stage_status_wins(env):
    """全部收工后按每阶段最新状态判定：旧失败历史不得拖垮已完成的新一轮。"""
    svc = QueueService()
    await _mk_task(env, 1, status="processing")
    await _mk_job(env, 1, "video", status="failed", attempts=3)     # 旧历史
    await _mk_job(env, 1, "subtitle", status="failed", attempts=0)  # 旧历史（级联）
    await _mk_job(env, 1, "image", status="done")
    await _mk_job(env, 1, "video", status="done")
    await _mk_job(env, 1, "subtitle", status="done")

    async with env() as s:
        await svc._maybe_finalize(s, 1)
        t = await s.get(Task, 1)
    assert t.status == "pending_review", "每阶段最新状态全部 done → 待审核"
