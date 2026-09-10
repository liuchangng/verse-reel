"""测试禁止污染生产库 守护测试（2026-09-09「莫名又重新生成」事故）

事故复盘（完整证据链）：
1. 用户反馈"我没点重新生成，为啥又重新生成了"——任务 1 的分镜图/视频被反复重跑。
2. 现象：每次跑完全量测试，generation_jobs 就多出 task 1 的 image+video 两条
   pending Job，且 clear_outputs 把已生成产物清空。
3. 根因：``tests/test_tasks_review.py::test_regenerate_sets_processing`` 直接调
   ``regenerate_task(1, stage="image", db=FakeDb(...))``。FakeDb 只骗过了函数
   参数，函数内部 ``queue_service.enqueue_task`` 走的仍是
   ``app.services.queue.async_session_factory`` —— 真实 poems.db（3.8G）。
   即：**跑一次测试 = 对用户的任务 1 执行一次真实的"重新生成"**。
4. 修复：
   - conftest 全局 autouse 把队列会话工厂重定向到独立临时库（本文件锁定该规则）；
   - test_tasks_review 显式 monkeypatch 掉 enqueue_task；
   - enqueue_task 增加 source 审计 + 覆盖在途 Job 时打 WARNING。

本测试锁住第 4 条第 1 点：任何用例都不允许在未显式声明的情况下打到生产库。
"""
import pytest


def test_queue_uses_isolated_db_by_default():
    """默认隔离：队列模块的会话工厂不得指向真实库。"""
    import app.services.queue as qmod
    from app.database import async_session_factory as real_factory

    assert qmod.async_session_factory is not real_factory, (
        "队列服务的会话工厂被指回生产库 —— 这会让‘跑测试’变成‘重新生成用户的任务’。"
        "需要生产库的用例必须显式 monkeypatch 并自行清理。"
    )


@pytest.mark.asyncio()
async def test_enqueue_into_isolated_db_leaves_production_untouched():
    """往隔离库入队后，生产库 generation_jobs 行数不变（端到端验证不串库）。"""
    from sqlalchemy import func, select

    import app.services.queue as qmod
    from app.database import async_session_factory as real_factory
    from app.models.job import Job
    from app.models.task import Task
    from app.services.queue import QueueService

    async with real_factory() as db:
        before = (await db.execute(select(func.count()).select_from(Job))).scalar()

    # 隔离库里建一个 processing 任务并入队（走隔离工厂，不碰生产库）
    async with qmod.async_session_factory() as s:
        s.add(Task(id=4242, poem_id=1, platform="douyin", status="processing"))
        await s.commit()

    await QueueService().enqueue_task(task_id=4242, stages=["image"], source="isolation-test")

    async with qmod.async_session_factory() as s:
        rows = (await s.execute(select(Job).where(Job.task_id == 4242))).scalars().all()
    # 空任务入队 image 会补全前置 script/character → 3 条；关键是它们全在隔离库
    assert {j.stage for j in rows} == {"script", "character", "image"}, \
        "Job 必须落在隔离库（且不串到生产库）"

    async with real_factory() as db:
        after = (await db.execute(select(func.count()).select_from(Job))).scalar()
    assert before == after, "生产库 generation_jobs 行数不得因测试而变化"
