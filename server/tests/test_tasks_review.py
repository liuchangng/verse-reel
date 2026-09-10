"""任务审核/重新生成端点单元测试（mock db）

⚠️ 2026-09-09 事故：本文件曾对**生产库**造成真实写入。
``regenerate_task`` 内部会调 ``queue_service.enqueue_task``，后者用的是
``app.services.queue.async_session_factory``（真实 poems.db），而这里的 db 是
FakeDb —— 于是"单元测试"每次跑都真的给任务 1 入队 image/video 并清空产物，
表现到前端就是"我没点重新生成，图片怎么又变了一批"。
修复：入队必须 monkeypatch（另有 tests/conftest.py 的全局隔离护栏兜底）。
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.api.tasks as tasks_api
from app.api.tasks import review_task, regenerate_task
from app.models.task import Task
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def _fake_patch_task(monkeypatch):
    """把 patch_task（真 SQL CAS）替换成语义等价的内存实现。

    2026-09-10 并发加固后 review/regenerate 内部走 ``patch_task`` 列级原子
    UPDATE；FakeDb 不是真 session，无法承载 SQL —— 这里直接把字段应用到
    FakeTask，保持本文件「只测端点状态机语义」的职责不变（CAS 语义另见
    tests/test_task_state_cas.py）。
    """
    async def fake_patch(session, task_id, *, only_if=None, **fields):
        for k, v in fields.items():
            setattr(session._task, k, v)
        return True

    monkeypatch.setattr(tasks_api, "patch_task", fake_patch)


class FakeTask:
    """模拟 SQLAlchemy Task 对象（带 review 字段）"""
    def __init__(self, status="pending_review"):
        self.id = 1
        self.poem_id = 100
        self.status = status
        self.current_stage = "review"
        self.progress = 95
        self.review_status = "pending"
        self.review_comment = None
        self.reviewed_at = None
        self.completed_at = None
        self.error_message = None


class FakeResult:
    """模拟 SQLAlchemy Result：.scalars().all() —— 供在途 Job 查询使用（空=无在途）"""
    def __init__(self, rows=None):
        self._rows = rows or []

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class FakeDb:
    """模拟 AsyncSession：get/commit/refresh/execute"""
    def __init__(self, task):
        self._task = task
        self.committed = False
        self.refreshed = False

    async def get(self, model, task_id):
        return self._task

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        self.refreshed = True

    async def execute(self, *a, **kw):
        """在途 Job 查询：默认返回空（无在途 Job → 重新生成放行）。"""
        return FakeResult()


class TestReviewTask:
    """人工审核端点"""

    @pytest.mark.asyncio
    async def test_approve_sets_done(self):
        """approve → status=done + review_status=approved + reviewed_at"""
        task = FakeTask("pending_review")
        db = FakeDb(task)
        resp = await review_task(1, action="approve", db=db)
        assert resp["status"] == "done"
        assert task.review_status == "approved"
        assert task.completed_at is not None
        assert 'reviewed_at' in resp

    @pytest.mark.asyncio
    async def test_reject_sets_failed(self):
        """reject → status=failed + review_status=rejected + comment 保存"""
        task = FakeTask("pending_review")
        db = FakeDb(task)
        resp = await review_task(1, action="reject", comment="内容不合适", db=db)
        assert resp["status"] == "failed"
        assert task.review_status == "rejected"
        assert task.review_comment == "内容不合适"

    @pytest.mark.asyncio
    async def test_non_pending_review_returns_400(self):
        """非 pending_review 调用 review → 400"""
        task = FakeTask("processing")
        db = FakeDb(task)
        with pytest.raises(HTTPException) as exc:
            await review_task(1, action="approve", db=db)
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_action_returns_400(self):
        """非法 action → 400"""
        task = FakeTask("pending_review")
        db = FakeDb(task)
        with pytest.raises(HTTPException) as exc:
            await review_task(1, action="publish", db=db)
        assert exc.value.status_code == 400


class TestRegenerateTask:
    """重新生成端点"""

    @pytest.fixture()
    def no_real_enqueue(self, monkeypatch):
        """拦掉真实入队：regenerate_task 内部会走 queue_service，绝不能打到生产库。"""
        from app.services.queue import queue_service

        calls = []

        async def fake_enqueue(task_id, stages=None, clear_outputs=False, source="unknown"):
            calls.append({
                "task_id": task_id, "stages": stages,
                "clear_outputs": clear_outputs, "source": source,
            })
            return len(stages) if stages else 6

        monkeypatch.setattr(queue_service, "enqueue_task", fake_enqueue)
        return calls

    @pytest.mark.asyncio
    async def test_regenerate_sets_processing(self, no_real_enqueue):
        """regenerate → status=processing + progress=0"""
        task = FakeTask("pending_review")
        db = FakeDb(task)
        resp = await regenerate_task(1, stage="image", force=False, db=db)
        assert resp["status"] == "processing"
        assert task.status == "processing"
        assert task.progress == 0
        assert task.review_status == "pending"
        # 入队走 mock（若这里漏了 mock，就会真的给生产库任务1重跑一遍图片）
        assert no_real_enqueue and no_real_enqueue[0]["stages"] == ["image"]
        assert no_real_enqueue[0]["source"] == "regenerate"

    @pytest.mark.asyncio
    async def test_regenerate_valid_stage(self, no_real_enqueue):
        """合法 stage 不报错"""
        task = FakeTask("failed")
        db = FakeDb(task)
        resp = await regenerate_task(1, stage="video", force=False, db=db)
        assert resp["status"] == "processing"
        assert no_real_enqueue[0]["stages"] == ["video"]
