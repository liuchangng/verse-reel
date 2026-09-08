"""共享 fixture（test-health-20260908 REQ-2：鉴权 fixture 三文件复用去重）。"""
import pytest

from app.config import settings


@pytest.fixture()
def enable_auth(monkeypatch):
    """开启鉴权：设置 app_token。"""
    monkeypatch.setattr(settings, "app_token", "test-token")
    return "test-token"


@pytest.fixture()
def auth_headers(enable_auth):
    return {"Authorization": f"Bearer {enable_auth}"}
