"""创建任务必须走队列 守护测试（2026-09-09 进度弹窗"暂无执行记录"事故）

事故：POST /api/tasks/ 用 BackgroundTasks 直跑 run_pipeline，完全绕过队列——
generation_jobs 无记录（进度弹窗空）、不受任务间串行约束、后端重启即死
（僵尸 processing，subtitle 阶段进行中进程被重启后无人推进）。
修复：创建即 enqueue_task 全套阶段，与 regenerate/batch 同一架构。

直接以函数调用方式测 API 端点（Depends 默认值不影响直传 db），
内存库 + monkeypatch 掉 enqueue_task，不触碰真实队列/数据库。
"""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.api.tasks import create_task
from app.database import Base
from app.models.poem import Poem
from app.models.task import Task
from app.services.queue import STAGE_ORDER
import app.api.tasks as tasks_api


@pytest_asyncio.fixture()
async def api_env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    calls = []

    async def fake_enqueue(task_id, stages=None, clear_outputs=False, source="unknown"):
        calls.append({"task_id": task_id, "stages": stages, "source": source})
        return len(stages) if stages else len(STAGE_ORDER)

    monkeypatch.setattr(tasks_api.queue_service, "enqueue_task", fake_enqueue)
    yield factory, calls
    await engine.dispose()


@pytest.mark.asyncio()
async def test_create_task_enqueues_all_stages(api_env):
    """创建任务必须入队全套阶段（产生执行记录），不得直跑 run_pipeline。"""
    factory, calls = api_env
    async with factory() as db:
        db.add(Poem(id=1, title="静夜思", author="李白", dynasty="唐", content="床前明月光"))
        await db.commit()

        resp = await create_task(poem_id=1, platform="douyin", platforms=None, db=db)

    assert len(calls) == 1, "create_task 应恰好入队一次"
    assert calls[0]["task_id"] == resp["id"]
    assert resp["enqueued_count"] == len(STAGE_ORDER)
    # 审计：入队必须自报来源，否则"任务莫名又重新生成"无法追溯
    assert calls[0]["source"] == "create_task"

    async with factory() as db:
        task = (await db.execute(select(Task).where(Task.id == resp["id"]))).scalar_one()
        assert task.status == "pending", "入队后任务应保持 pending，由队列推进状态"
        # 热点来源透传（上一轮修复回归保护）
        task2 = await create_task(
            poem_id=1, platform="douyin", platforms=None,
            source_hotspot_title="重阳节登高",
            source_keywords=["重阳", "登高"], db=db,
        )
    assert len(calls) == 2 and calls[1]["task_id"] == task2["id"]
