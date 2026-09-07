"""安全加固守护测试：密钥脱敏 / confirm 防护 / 接口限频 / delete 清产物。

req: REQ-S1 (mask_api_key)
req: REQ-S3 (confirm + 限频)
req: REQ-S4 (delete_task 清产物)
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app, mask_api_key
from app.config import settings


@pytest.fixture()
def enable_auth(monkeypatch):
    """开启鉴权：设置 app_token。"""
    monkeypatch.setattr(settings, "app_token", "test-token")
    return "test-token"


@pytest.fixture()
def auth_headers(enable_auth):
    return {"Authorization": f"Bearer {enable_auth}"}


# ---------- REQ-S1 密钥脱敏 ----------

class TestMaskApiKey:
    def test_empty_key_returns_unconfigured(self):
        masked, configured = mask_api_key("")
        assert masked == ""
        assert configured is False

    def test_normal_key_masked(self):
        masked, configured = mask_api_key("sk-abcdefghij1234")
        assert configured is True
        assert "sk-ab" in masked       # 前 5 字符保留
        assert masked.endswith("1234")  # 尾 4 字符保留
        assert "****" in masked
        assert "abcdefghij" not in masked  # 中段不泄露

    def test_short_key_not_leaked(self):
        masked, configured = mask_api_key("ab")
        assert configured is True
        assert masked == "a****"  # 短 key 只留首字符


# ---------- REQ-S3 高危接口 confirm 防护 ----------

class TestConfirmGuard:
    def test_batch_clear_no_confirm_400(self, auth_headers):
        """batch-clear 不带 confirm=YES → 400（在删除逻辑之前拦截，无副作用）。"""
        with TestClient(app) as client:
            r = client.delete("/api/tasks/batch-clear", headers=auth_headers)
            assert r.status_code == 400

    def test_publish_no_confirm_400(self, auth_headers):
        """publish 不带 confirm=YES → 400（在业务查询之前拦截）。"""
        with TestClient(app) as client:
            r = client.post("/api/tasks/999999/publish", headers=auth_headers)
            assert r.status_code == 400

    def test_publish_with_confirm_passes_to_404(self, auth_headers):
        """publish 带 confirm=YES → 通过校验，进入业务（task 不存在 → 404，证明已过 confirm）。"""
        with TestClient(app) as client:
            r = client.post("/api/tasks/999999/publish?confirm=YES", headers=auth_headers)
            assert r.status_code == 404


# ---------- REQ-S3 接口限频 ----------

class TestRateLimit:
    async def test_limiter_timeout_zero_returns_false(self):
        """限频器 acquire(timeout=0) 满窗立即返回 False（429 判定依据）。"""
        from app.services.rate_limiter import RateLimiter
        lim = RateLimiter(rpm=2, name="test")
        assert await lim.acquire(timeout=0) is True
        assert await lim.acquire(timeout=0) is True
        assert await lim.acquire(timeout=0) is False  # 满窗拒绝

    def test_test_concurrency_429_when_limited(self, auth_headers, monkeypatch):
        """test-concurrency 超频 → 429（monkeypatch 限频器拒绝）。"""
        from app.services import rate_limiter as rl_mod

        async def _deny(*a, **k):
            return False

        monkeypatch.setattr(rl_mod.api_limiter_heavy, "acquire", _deny)
        with TestClient(app) as client:
            r = client.get("/api/settings/test-concurrency?type=text&concurrency=1", headers=auth_headers)
            assert r.status_code == 429
