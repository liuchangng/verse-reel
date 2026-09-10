"""task 状态原子更新（CAS）守护测试（2026-09-10 并发加固 P1-D）

背景：tasks 行的状态字段（status/current_stage/progress/review_status）有多个
写者——pipeline 各阶段、队列终态判定、REST API（审核/重新生成）。ORM 的
「get → 改属性 → commit」在两个写者交错时互相覆盖（lost update），典型表现：
用户点"审核通过"被并发的终态写入静默回滚成 pending_review。

修复：``app/services/task_state.patch_task`` 提供列级原子 UPDATE + ``only_if``
乐观条件。本文件锁定三层语义：工具本身、审核端点 409、重新生成端点 409。
"""
import pytest
from fastapi import HTTPException

import app.api.tasks as tasks_api
import app.services.queue as qmod
from app.models.task import Task
from app.services.task_state import patch_task


async def _make_task(task_id: int, status: str = "pending_review") -> None:
    async with qmod.async_session_factory() as s:
        s.add(Task(id=task_id, poem_id=1, platform="douyin", status=status, progress=95))
        await s.commit()


@pytest.mark.asyncio()
async def test_patch_task_only_if_hits_and_misses():
    """only_if 命中则写入成功；条件不再满足时不写入、不覆盖他人修改。"""
    await _make_task(6201)

    async with qmod.async_session_factory() as s:
        ok = await patch_task(
            s, 6201, only_if={"status": "pending_review"},
            status="done", progress=100,
        )
    assert ok is True, "条件命中应返回 True"

    async with qmod.async_session_factory() as s:
        t = await s.get(Task, 6201)
        assert t.status == "done" and t.progress == 100

        # 以过期条件写入 → 不命中，status 不得被改写
        ok = await patch_task(s, 6201, only_if={"status": "pending_review"}, status="failed")
        assert ok is False, "条件不满足必须返回 False（防覆盖）"

    async with qmod.async_session_factory() as s:
        t = await s.get(Task, 6201)
        assert t.status == "done", "过期条件的写入不得生效"


@pytest.mark.asyncio()
async def test_review_conflict_when_status_changed_concurrently(monkeypatch):
    """审核写入前 status 被并发写者改掉 → 返回 409 而不是静默覆盖。"""
    await _make_task(6202)

    real_patch = tasks_api.patch_task

    async def racing_patch(session, task_id, **kw):
        # 模拟并发写者（如重新生成/终态判定）抢先改写 status
        await real_patch(session, task_id, status="processing", progress=0)
        return await real_patch(session, task_id, **kw)

    monkeypatch.setattr(tasks_api, "patch_task", racing_patch)

    async with qmod.async_session_factory() as s:
        with pytest.raises(HTTPException) as ei:
            await tasks_api.review_task(6202, action="approve", comment=None, db=s)
    assert ei.value.status_code == 409, "状态被并发改写时应 409，让前端刷新"


@pytest.mark.asyncio()
async def test_regenerate_conflict_when_status_changed(monkeypatch):
    """重新生成写入前 status 被并发写者改掉 → 返回 409。"""
    await _make_task(6203, status="failed")

    async def failing_patch(session, task_id, **kw):
        return False  # 模拟 CAS 不命中（status 已被并发操作改变）

    monkeypatch.setattr(tasks_api, "patch_task", failing_patch)

    async with qmod.async_session_factory() as s:
        with pytest.raises(HTTPException) as ei:
            await tasks_api.regenerate_task(6203, stage="script", force=False, db=s)
    assert ei.value.status_code == 409
