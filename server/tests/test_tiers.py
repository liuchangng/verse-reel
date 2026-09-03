"""video-comm 文案改造轮 W1/W2 单测：档位档案 + S 快档评审卡在位守卫。

覆盖（纯离线，无 LLM/网络/ffmpeg 依赖）：
- W1 tier 档案：TIER_PROFILES 窗口与设计文档 §三 一致、PLATFORM_TIER 映射、
  tier_of/tier_profile 兜底、PLATFORM_CONFIG 每平台 duration_tier 与档案一致。
- W2 评审卡：S 档单套卡在位（新六维键 + 收藏动机 + 字数软扣分规则 + 旧错位键退役）。
"""
import pytest

from app.config import TIER_PROFILES, PLATFORM_TIER, tier_of, tier_profile
from app.services.pipeline import PLATFORM_CONFIG
from app.services.critic import CRITIC_SYSTEM_PROMPT


class TestTierProfiles:
    """W1：档位档案窗口值（对齐 design 2026-09-03 §三 档位表）。"""

    def test_s_tier_windows_match_design(self):
        p = TIER_PROFILES["S"]
        assert (p["chars_min"], p["chars_max"]) == (80, 130)
        assert (p["shots_min"], p["shots_max"]) == (6, 9)
        assert (p["dur_min"], p["dur_max"]) == (25, 40)
        assert p["dur_hard_max"] == 50

    def test_l_tier_windows_match_design(self):
        p = TIER_PROFILES["L"]
        assert (p["chars_min"], p["chars_max"]) == (300, 450)
        assert (p["shots_min"], p["shots_max"]) == (14, 22)
        assert (p["dur_min"], p["dur_max"]) == (90, 150)

    def test_unknown_tier_falls_back_to_s(self):
        assert tier_profile("X") is TIER_PROFILES["S"]
        assert tier_profile(None) is TIER_PROFILES["S"]
        assert tier_profile("s") is TIER_PROFILES["S"]  # 大小写不敏感

    def test_platform_tier_mapping(self):
        # S 档三平台 + L 档两平台（决策 1 定稿口径）
        for plat in ("douyin", "kuaishou", "xiaohongshu"):
            assert PLATFORM_TIER[plat] == "S"
        for plat in ("bilibili", "youtube"):
            assert PLATFORM_TIER[plat] == "L"

    def test_tier_of_fallback(self):
        assert tier_of("douyin") == "S"
        assert tier_of("youtube") == "L"
        assert tier_of("weibo") == "S"   # 未知平台兜底 S
        assert tier_of(None) == "S"
        assert tier_of("") == "S"

    def test_platform_config_tier_consistent(self):
        # PLATFORM_CONFIG 每平台 duration_tier 与档案一致（单一事实源不漂移）
        for plat, cfg in PLATFORM_CONFIG.items():
            assert cfg["duration_tier"] == PLATFORM_TIER[plat], plat

    def test_platform_config_tier_resolves_profile(self):
        # 五平台都能从 duration_tier 解析到档案（防手滑填错档位名）
        for plat in PLATFORM_CONFIG:
            assert tier_profile(tier_of(plat))["label"] in ("快档", "深档")


class TestCriticCardS:
    """W2：S 快档评审卡在位守卫（防回退旧卡/旧错位键）。"""

    def test_new_six_dimension_keys_present(self):
        for key in ("hook_score", "empathy_score", "accuracy_score",
                    "save_motive_score", "rhythm_score", "originality_score"):
            assert key in CRITIC_SYSTEM_PROMPT, f"缺失评审键 {key}"

    def test_save_motive_dimension_present(self):
        # 收藏动机（2026 权重第一维度）必须入卡
        assert "收藏动机" in CRITIC_SYSTEM_PROMPT
        assert "权重30%" in CRITIC_SYSTEM_PROMPT       # 钩子升至 30（传播向卡）
        assert "权重10%" in CRITIC_SYSTEM_PROMPT        # 收藏/节奏/原创各 10

    def test_legacy_misaligned_keys_retired(self):
        # 旧键取自生成 prompt 段落名（rebrand/details/alignment/emotion），与维度错位
        for legacy in ("rebrand_score", "details_score", "alignment_score", "emotion_score"):
            assert legacy not in CRITIC_SYSTEM_PROMPT, f"旧错位键未退役: {legacy}"

    def test_char_budget_soft_rule_present(self):
        # 软约束：80–130 字区间 + 扣至多 0.5（不硬失败）
        assert "80–130" in CRITIC_SYSTEM_PROMPT
        assert "0.5" in CRITIC_SYSTEM_PROMPT

    def test_no_hello_style_hook_gate(self):
        # S 档钩子铁律：禁"大家好/今天讲"式铺垫
        assert "大家好" in CRITIC_SYSTEM_PROMPT
