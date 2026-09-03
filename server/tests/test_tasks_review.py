"""任务审核/重新生成端点单元测试（mock db）"""
import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api.tasks import review_task, regenerate_task
from app.models.task import Task
from fastapi import HTTPException


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


class FakeDb:
    """模拟 AsyncSession：get/commit/refresh"""
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

    @pytest.mark.asyncio
    async def test_regenerate_sets_processing(self):
        """regenerate → status=processing + progress=0"""
        task = FakeTask("pending_review")
        db = FakeDb(task)
        resp = await regenerate_task(1, stage="image", db=db)
        assert resp["status"] == "processing"
        assert task.status == "processing"
        assert task.progress == 0
        assert task.review_status == "pending"

    @pytest.mark.asyncio
    async def test_regenerate_valid_stage(self):
        """合法 stage 不报错"""
        task = FakeTask("failed")
        db = FakeDb(task)
        resp = await regenerate_task(1, stage="video", db=db)
        assert resp["status"] == "processing"
