"""安全加固守护测试：密钥脱敏 / confirm 防护 / 接口限频 / delete 清产物。

req: REQ-S1 (mask_api_key)
req: REQ-S3 (confirm + 限频)
req: REQ-S4 (delete_task 清产物)
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app, mask_api_key
from app.config import settings

# enable_auth / auth_headers 已抽取至 tests/conftest.py（test-health-20260908 REQ-2）


# ---------- REQ-S1 密钥脱敏 ----------

class TestMaskApiKey:
    def test_empty_key_returns_unconfigured(self):
        masked, configured = mask_api_key("")
        assert masked == ""
        assert configured is False

    def test_normal_key_masked(self):
        """契约格式：sk-****尾4（前 3 字符前缀 + **** + 尾 4），守护契约而非实现。"""
        masked, configured = mask_api_key("sk-abcdefghij1234")
        assert configured is True
        assert masked == "sk-****1234"
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

    # test-concurrency 端点已于 2026-09-09 删除（用户定夺：队列有重试兜底，并发实测无意义），
    # 对应 429 守护测试一并移除；限频器语义由上方 test_limiter_timeout_zero_returns_false 覆盖。


# ---------- REQ-S4 P1 质量项 ----------

class TestDebugDefault:
    def test_debug_default_false(self):
        """debug 默认 False（不依赖运行时 .env，校验 Pydantic 字段默认）。"""
        from app.config import Settings
        assert Settings.model_fields["debug"].default is False


class TestDeleteCleanup:
    def test_cleanup_removes_task_dir(self, monkeypatch, tmp_path):
        """删除任务后产物目录被清理（REQ-S4.5）。"""
        from app.api.tasks import _cleanup_task_output
        from app.config import settings
        monkeypatch.setattr(settings, "output_dir", str(tmp_path))
        d = tmp_path / "task_42"
        d.mkdir()
        (d / "final.mp4").write_text("x")
        _cleanup_task_output(42)
        assert not d.exists()

    def test_cleanup_missing_dir_no_error(self, monkeypatch, tmp_path):
        """产物目录不存在时清理不报错（无任务/已清理场景）。"""
        from app.api.tasks import _cleanup_task_output
        from app.config import settings
        monkeypatch.setattr(settings, "output_dir", str(tmp_path))
        _cleanup_task_output(999)  # 应静默通过
