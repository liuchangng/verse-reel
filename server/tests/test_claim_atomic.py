"""队列认领原子性 守护测试（2026-09-10 并发加固 P0-A）

背景（真实风险，非理论）：
``_claim_and_dispatch`` 旧写法是「SELECT 出 pending Job → 检查依赖 → 改 ORM 对象
→ commit」。SELECT 与 UPDATE 之间隔着多个 await 点，另一消费者可以在同一窗口读到
同一个 pending Job 并各自派发。多消费者的来源真实存在：

- ``start.bat`` 用 ``uvicorn --reload``，重启窗口内新旧子进程短暂并存；
- 误起第二个实例（不同端口）；
- 测试用 ``TestClient(app)`` 触发 lifespan（2026-09-09 已真实发生过）。

后果：同一阶段被跑两遍，产物互相覆盖（分镜图/视频被"莫名重生成"）。

修复：认领改为带 ``status='pending'`` 条件的原子 UPDATE，``rowcount == 1`` 才派发。
本文件锁定该语义。
"""
import asyncio

import pytest
from sqlalchemy import select

import app.services.queue as qmod
from app.models.job import Job
from app.models.task import Task
from app.services.queue import QueueService


async def _seed_pending_job(task_id: int, stage: str = "script") -> int:
    """在隔离库建一个无前置依赖的 pending Job，返回其 id。"""
    # 清场：隔离库是同一次 pytest 会话共用的，前面的用例可能留下在途 Job。
    # _claim_and_dispatch 的「任务间串行」会锁定 running Job 所属任务，
    # 残留会让本用例新 seed 的 Job 被过滤掉（表现为认领失败假阳性）。
    async with qmod.async_session_factory() as s:
        stale = (await s.execute(
            select(Job).where(Job.status.in_(("pending", "running")))
        )).scalars().all()
        for j in stale:
            await s.delete(j)
        await s.commit()

    async with qmod.async_session_factory() as s:
        s.add(Task(id=task_id, poem_id=1, platform="douyin", status="processing"))
        await s.commit()
        job = Job(task_id=task_id, stage=stage, status="pending", attempts=0, priority=60)
        s.add(job)
        await s.commit()
        await s.refresh(job)
        return job.id


@pytest.mark.asyncio()
async def test_concurrent_claim_only_one_wins():
    """两个消费者同一轮争抢同一个 pending Job → 只有一个认领成功、只派发一次。"""
    job_id = await _seed_pending_job(5101)

    q = QueueService()
    dispatched: list[int] = []

    async def fake_run(jid, sem):
        dispatched.append(jid)

    q._run_job = fake_run  # 不真跑流水线

    # 两个消费者并发调用（模拟双实例 / reload 重启窗口）
    results = await asyncio.gather(q._claim_and_dispatch(), q._claim_and_dispatch())
    await asyncio.sleep(0)  # 让 create_task 的协程真正跑起来

    assert sum(results) == 1, f"两个消费者只能有一个认领成功，实际 {results}"
    assert dispatched == [job_id], f"只允许派发一次，实际 {dispatched}"

    async with qmod.async_session_factory() as s:
        job = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one()
    assert job.status == "running", "胜出的消费者应把 Job 置 running"


@pytest.mark.asyncio()
async def test_second_claim_after_first_is_noop():
    """串行场景同样安全：已被认领的 Job 再扫一次不会被二次派发。"""
    job_id = await _seed_pending_job(5102)

    q = QueueService()
    dispatched: list[int] = []

    async def fake_run(jid, sem):
        dispatched.append(jid)

    q._run_job = fake_run

    first = await q._claim_and_dispatch()
    second = await q._claim_and_dispatch()

    assert first == 1, "首次应成功认领"
    assert second == 0, "已被 running 的 Job 不得再次认领"
    assert len(dispatched) == 1
    assert dispatched[0] == job_id
