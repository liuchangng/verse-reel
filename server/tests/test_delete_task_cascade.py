"""删除任务级联清理子表 守护测试（2026-09-09 孤儿 Job 饿死队列事故）

事故：delete_task 只删 tasks 行，漏删 generation_jobs / scripts。孤儿 Job
按优先级永远占据串行队列"活动任务"位且永远不被消费，把其他任务饿死 8h+。
（batch-clear 的清空接口早有级联，单任务删除漏了——本测试防回归。）

直接以函数调用方式测 API 端点（Depends 默认值不影响直传 db），
用独立内存库，不触碰真实数据库。
"""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.api.tasks import delete_task
from app.database import Base
from app.models.job import Job
from app.models.script import Script
from app.models.task import Task


@pytest_asyncio.fixture()
async def db_env():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio()
async def test_delete_task_removes_jobs_and_scripts(db_env):
    """删除任务必须级联清掉 generation_jobs 与 scripts，不留孤儿。"""
    factory = db_env
    async with factory() as s:
        s.add(Task(id=7, poem_id=1, platform="douyin"))
        s.add(Job(task_id=7, stage="script", status="done", priority=60))
        s.add(Job(task_id=7, stage="video", status="pending", priority=10))
        s.add(Script(task_id=7, full_script="旧文案"))
        # 干扰项：别的任务的数据不能被误删
        s.add(Task(id=8, poem_id=1, platform="douyin"))
        s.add(Job(task_id=8, stage="script", status="done", priority=60))
        s.add(Script(task_id=8, full_script="别人的文案"))
        await s.commit()

    resp = await delete_task(task_id=7, db=s)
    assert resp["message"] == "任务已删除"

    async with factory() as s:
        assert await s.get(Task, 7) is None
        assert (await s.execute(
            select(Job).where(Job.task_id == 7))).scalars().all() == [], \
            "task7 的 Job 必须级联删除"
        assert (await s.execute(
            select(Script).where(Script.task_id == 7))).scalars().all() == [], \
            "task7 的 scripts 必须级联删除"
        # 其他任务数据完好
        assert (await s.execute(
            select(Job).where(Job.task_id == 8))).scalars().all(), "task8 数据不得误删"
        assert (await s.execute(
            select(Script).where(Script.task_id == 8))).scalars().all()
