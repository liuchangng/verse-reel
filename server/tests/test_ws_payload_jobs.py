"""WS 推送载荷合并 Job 状态 守护测试（2026-09-10 /jobs 刷屏修复）

事故：后端 WS 每秒推一次 progress，前端每收到一条就回源
``GET /tasks/{id}/jobs`` 刷新步骤条 → 后端日志每秒多一条 HTTP 请求。
修复：``_build_progress_payload`` 把 6 阶段 Job 状态并入推送载荷
（``jobs``：stage -> status，id 升序遍历每阶段留最新），前端零回源。
"""
import pytest

import app.api.ws as ws_mod
import app.services.queue as qmod
from app.models.job import Job
from app.models.task import Task


@pytest.fixture(autouse=True)
def _isolate_ws_db(monkeypatch):
    """WS 模块直接 import 生产库工厂 → 重定向到隔离库（同 conftest 护栏口径）。"""
    monkeypatch.setattr(ws_mod, "async_session_factory", qmod.async_session_factory)


async def _seed(task_id: int, jobs: list[tuple[str, str]]):
    """隔离库 seed：task + 按 id 顺序追加的 stage/status Job 列表。"""
    async with ws_mod.async_session_factory() as s:
        s.add(Task(id=task_id, poem_id=1, platform="douyin", status="processing",
                   current_stage="video", progress=60))
        await s.commit()
        for stage, status in jobs:
            s.add(Job(task_id=task_id, stage=stage, status=status, attempts=0, priority=50))
        await s.commit()


@pytest.mark.asyncio()
async def test_payload_contains_jobs_map(monkeypatch):
    """载荷带 jobs 映射：6 阶段 -> 最新状态。"""
    await _seed(7101, [
        ("script", "done"), ("character", "done"), ("image", "done"),
        ("tts", "done"), ("video", "running"),
    ])

    async with ws_mod.async_session_factory() as s:
        payload = await ws_mod._build_progress_payload(s, 7101)

    assert payload is not None
    assert payload["type"] == "progress"
    assert payload["status"] == "processing"
    assert payload["jobs"] == {
        "script": "done", "character": "done", "image": "done",
        "tts": "done", "video": "running",
    }


@pytest.mark.asyncio()
async def test_jobs_map_keeps_latest_per_stage(monkeypatch):
    """同一阶段多条 Job（重新生成保留历史）→ 只留最新一条（id 最大）。"""
    await _seed(7102, [("image", "done"), ("image", "failed"), ("image", "pending")])

    async with ws_mod.async_session_factory() as s:
        payload = await ws_mod._build_progress_payload(s, 7102)

    assert payload["jobs"] == {"image": "pending"}


@pytest.mark.asyncio()
async def test_payload_none_when_task_missing():
    """任务不存在 → None（推送循环静默跳过，不报错）。"""
    async with ws_mod.async_session_factory() as s:
        assert await ws_mod._build_progress_payload(s, 987654) is None
