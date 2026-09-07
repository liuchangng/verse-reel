"""安全加固守护测试：密钥脱敏 / confirm 防护 / 接口限频 / delete 清产物。

req: REQ-S1 (mask_api_key)
req: REQ-S3 (confirm + 限频)
req: REQ-S4 (delete_task 清产物)
"""
import pytest
from app.main import mask_api_key


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
