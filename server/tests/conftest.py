"""共享 fixture（test-health-20260908 REQ-2：鉴权 fixture 三文件复用去重）。"""
import os
import tempfile

import pytest

from app.config import settings

# ------------------------------------------------------------------ #
# 全局护栏：队列服务的会话工厂一律重定向到独立临时库（2026-09-09 事故）
#
# 事故：tests/test_tasks_review.py 直接调 ``regenerate_task(1, stage="image")``，
# 传入的 db 是 FakeDb，但函数内部的 ``queue_service.enqueue_task`` 用的是
# ``app.services.queue.async_session_factory`` —— 也就是生产 poems.db。
# 结果：每跑一次全量测试，就真的给任务 1 入队 image/video 并 clear_outputs
# 清空产物，前端看到"我没点重新生成，图片怎么又变了一批"。
# 规则：测试默认不得触碰生产库；需要生产库的用例必须显式声明并自行接管。
# ------------------------------------------------------------------ #
_ISOLATED_FACTORY = None


def pytest_configure(config):
    """建立一次性的隔离库（全 schema DDL），供所有测试复用。"""
    global _ISOLATED_FACTORY
    from sqlalchemy import create_engine
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

    from app.database import Base
    # 必须先导入模型模块，否则 Base.metadata 里没有对应表（create_all 建空库）
    from app.models.job import Job  # noqa: F401
    from app.models.poem import Poem  # noqa: F401
    from app.models.script import Script  # noqa: F401
    from app.models.task import Task  # noqa: F401

    fd, path = tempfile.mkstemp(prefix="queue-test-", suffix=".db")
    os.close(fd)
    sync_engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    _ISOLATED_FACTORY = async_sessionmaker(
        create_async_engine(f"sqlite+aiosqlite:///{path}"), expire_on_commit=False,
    )


@pytest.fixture(autouse=True)
def _isolate_queue_db(monkeypatch):
    """每个用例默认把队列会话工厂指向隔离库（用例可自行 monkeypatch 覆盖）。"""
    import app.services.queue as qmod

    monkeypatch.setattr(qmod, "async_session_factory", _ISOLATED_FACTORY)


@pytest.fixture()
def enable_auth(monkeypatch):
    """开启鉴权：设置 app_token。"""
    monkeypatch.setattr(settings, "app_token", "test-token")
    return "test-token"


@pytest.fixture()
def auth_headers(enable_auth):
    return {"Authorization": f"Bearer {enable_auth}"}
