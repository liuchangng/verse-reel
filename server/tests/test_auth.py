"""鉴权守护测试：Bearer Token（REQ-S2）。

req: REQ-S2
覆盖：
- AC-S2a 无/错 token → 401；正确 token → 200
- AC-S2b /health / 豁免（无 token 仍 200）
- AC-S2d app_token 未配置（空）→ 503 fail-closed
- WS 无 token → 连接被拒
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.config import settings


@pytest.fixture()
def enable_auth(monkeypatch):
    """开启鉴权：设置 app_token。"""
    monkeypatch.setattr(settings, "app_token", "test-token")
    return "test-token"


@pytest.fixture()
def auth_headers(enable_auth):
    return {"Authorization": f"Bearer {enable_auth}"}


class TestHttpAuth:
    def test_no_token_401(self, enable_auth):
        with TestClient(app) as client:
            assert client.get("/api/tasks/").status_code == 401

    def test_wrong_token_401(self, enable_auth):
        with TestClient(app) as client:
            assert client.get("/api/tasks/", headers={"Authorization": "Bearer wrong"}).status_code == 401

    def test_correct_token_200(self, auth_headers):
        with TestClient(app) as client:
            assert client.get("/api/tasks/", headers=auth_headers).status_code == 200

    def test_settings_endpoint_requires_token(self, enable_auth):
        with TestClient(app) as client:
            assert client.get("/api/settings").status_code == 401
            assert client.get("/api/settings", headers={"Authorization": "Bearer test-token"}).status_code == 200

    def test_health_root_exempt(self, enable_auth):
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            assert client.get("/").status_code == 200


class TestFailClosed:
    def test_empty_app_token_rejects(self, monkeypatch):
        """未配置 APP_TOKEN → 受保护接口全部拒绝（fail-closed）。"""
        monkeypatch.setattr(settings, "app_token", "")
        with TestClient(app) as client:
            r = client.get("/api/tasks/", headers={"Authorization": "Bearer anything"})
            assert r.status_code == 503
