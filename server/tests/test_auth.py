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


class TestOutputsAuth:
    """review IMPORTANT-1：/outputs 产物目录必须挂鉴权（原 StaticFiles 裸挂载不继承依赖）。"""

    def test_outputs_no_token_401(self, enable_auth):
        with TestClient(app) as client:
            assert client.get("/outputs/task_1/final.mp4").status_code == 401

    def test_outputs_empty_app_token_503(self, monkeypatch):
        """fail-closed 对产物目录同样成立。"""
        monkeypatch.setattr(settings, "app_token", "")
        with TestClient(app) as client:
            assert client.get("/outputs/task_1/final.mp4").status_code == 503

    def test_outputs_with_token_serves_file(self, auth_headers, monkeypatch, tmp_path):
        """带合法 token → 正常返回文件内容（功能不回退）。"""
        monkeypatch.setattr(settings, "output_dir", str(tmp_path))
        f = tmp_path / "task_1_final.mp4"
        f.write_text("video-bytes")
        with TestClient(app) as client:
            r = client.get("/outputs/task_1_final.mp4", headers=auth_headers)
            assert r.status_code == 200
            assert r.content == b"video-bytes"

    def test_outputs_query_token_serves_file(self, enable_auth, monkeypatch, tmp_path):
        """浏览器 <video>/<img> 无法带 Authorization 头 → 接受 ?token= 查询参数（与 WS 同模式）。"""
        monkeypatch.setattr(settings, "output_dir", str(tmp_path))
        f = tmp_path / "task_2_final.mp4"
        f.write_text("qv")
        with TestClient(app) as client:
            r = client.get("/outputs/task_2_final.mp4?token=test-token")
            assert r.status_code == 200
            assert r.content == b"qv"

    def test_outputs_wrong_query_token_401(self, enable_auth):
        with TestClient(app) as client:
            assert client.get("/outputs/x.mp4?token=wrong").status_code == 401

    def test_outputs_traversal_blocked(self, auth_headers, monkeypatch, tmp_path):
        """目录穿越（%2e%2e）→ 404，不得逃出产物目录。"""
        monkeypatch.setattr(settings, "output_dir", str(tmp_path))
        secret = tmp_path.parent / "secret.txt"
        secret.write_text("top-secret")
        with TestClient(app) as client:
            r = client.get("/outputs/%2e%2e%2fsecret.txt", headers=auth_headers)
            assert r.status_code == 404


class TestWsAuth:
    """review MINOR-4：WS close(4401) 分支补测试守护（原 docstring 声称覆盖但无用例）。"""

    def test_ws_no_token_rejected_4401(self, enable_auth):
        from fastapi import WebSocketDisconnect

        with TestClient(app) as client:
            with pytest.raises(WebSocketDisconnect) as ei:
                with client.websocket_connect("/ws/progress/1"):
                    pass
            assert ei.value.code == 4401

    def test_ws_wrong_token_rejected_4401(self, enable_auth):
        from fastapi import WebSocketDisconnect

        with TestClient(app) as client:
            with pytest.raises(WebSocketDisconnect) as ei:
                with client.websocket_connect("/ws/progress/1?token=wrong"):
                    pass
            assert ei.value.code == 4401
