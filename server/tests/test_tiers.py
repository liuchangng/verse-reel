"""video-comm 文案改造轮 W1/W2/W3 单测：档位档案 + S 快档评审卡 + 档位文案模板在位守卫。

覆盖（纯离线，无 LLM/网络/ffmpeg 依赖）：
- W1 tier 档案：TIER_PROFILES 窗口与设计文档 §三 一致、PLATFORM_TIER 映射、
  tier_of/tier_profile 兜底、PLATFORM_CONFIG 每平台 duration_tier 与档案一致。
- W2 评审卡：S 档单套卡在位（新六维键 + 收藏动机 + 字数软扣分规则 + 旧错位键退役）。
- W3 文案档位化：tier_script_guidelines 结构模板与数字注入（对齐 §四 三段式 + TIER_PROFILES）、
  resolve_task_tier 任务级档位解析（含混合收敛 S）、CREATOR_SYSTEM_PROMPT 默认=S 档防回退。
"""
import pytest
import re

from app.config import (
    TIER_PROFILES, PLATFORM_TIER, tier_of, tier_profile,
    tier_script_guidelines, resolve_task_tier, settings,
)
from app.services.pipeline import PLATFORM_CONFIG
from app.services.critic import CRITIC_SYSTEM_PROMPT, CREATOR_SYSTEM_PROMPT


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


class TestTierScriptGuidelines:
    """W3：档位文案结构模板在位守卫（对齐 design 2026-09-03 §四 三段式 / 五段式）。"""

    def test_s_three_act_structure_present(self):
        t = tier_script_guidelines("S")
        for kw in ("三段式", "金句钩子", "白话直给", "现代对齐", "金句句界", "收尾"):
            assert kw in t, f"S 档模板缺 {kw}"

    def test_s_chars_injected_from_profile(self):
        # 文本内字数区间由 TIER_PROFILES 注入（数字单一事实源防漂移）
        m = re.search(r"(\d+)–(\d+) 字", tier_script_guidelines("S"))
        assert m is not None
        assert (int(m.group(1)), int(m.group(2))) == (
            TIER_PROFILES["S"]["chars_min"], TIER_PROFILES["S"]["chars_max"])

    def test_s_no_legacy_five_act_leftover(self):
        # 旧文案 prompt（五段式 + 300–400 字/2 分钟）在 S 档必须绝迹
        t = tier_script_guidelines("S")
        for legacy in ("痛点钩子", "人设重塑", "电影级细节", "灵魂对齐", "情绪出口",
                       "300-400", "300–400", "2分钟"):
            assert legacy not in t, f"S 档模板残留旧结构: {legacy}"

    def test_s_golden_line_boundary_rule(self):
        # 金句句界铁律：原诗整句 + 「」标注 + 禁改写冒充
        t = tier_script_guidelines("S")
        assert "「」" in t or "「" in t
        assert "整句" in t and "原文" in t

    def test_l_five_act_structure_present(self):
        t = tier_script_guidelines("L")
        for kw in ("五段式", "痛点钩子", "人设重塑", "电影级细节", "灵魂对齐", "情绪出口"):
            assert kw in t, f"L 档模板缺 {kw}"
        m = re.search(r"(\d+)–(\d+) 字", t)
        assert m is not None
        assert (int(m.group(1)), int(m.group(2))) == (
            TIER_PROFILES["L"]["chars_min"], TIER_PROFILES["L"]["chars_max"])

    def test_unknown_tier_defaults_s(self):
        assert tier_script_guidelines(None) == tier_script_guidelines("S")
        assert tier_script_guidelines("X") == tier_script_guidelines("S")

    def test_creator_system_prompt_is_s_default(self):
        # critic 默认文案 prompt = config tier S 档位段（防回退旧五段式常量）
        assert CREATOR_SYSTEM_PROMPT == tier_script_guidelines("S")
        assert "2分钟" not in CREATOR_SYSTEM_PROMPT


class TestTaskTierResolve:
    """W3：任务级档位解析（决策 1 推论：任务创建按平台解析 duration_tier）。"""

    def test_pure_s_platforms(self):
        assert resolve_task_tier(["douyin"]) == "S"
        assert resolve_task_tier(["douyin", "kuaishou", "xiaohongshu"]) == "S"

    def test_pure_l_platforms(self):
        assert resolve_task_tier(["bilibili"]) == "L"
        assert resolve_task_tier(["bilibili", "youtube"]) == "L"

    def test_mixed_converges_to_s(self):
        # S+L 混合收敛 S（L 长文案发 S 平台不可接受；S 短文案发 L 平台可接受）
        assert resolve_task_tier(["douyin", "bilibili"]) == "S"
        assert resolve_task_tier(["xiaohongshu", "youtube", "kuaishou"]) == "S"

    def test_empty_and_unknown(self):
        assert resolve_task_tier([]) in ("S", "L")
        assert resolve_task_tier(["weibo"]) == "S"  # 未知平台兜底 S

    def test_none_falls_back_to_output_platforms(self, monkeypatch):
        # 空平台 → settings.output_platforms（默认四平台含 bilibili → 混合收敛 S）
        monkeypatch.setattr(settings, "output_platforms",
                            ["douyin", "xiaohongshu", "kuaishou", "bilibili"])
        assert resolve_task_tier(None) == "S"
        monkeypatch.setattr(settings, "output_platforms", ["bilibili", "youtube"])
        assert resolve_task_tier(None) == "L"
        monkeypatch.setattr(settings, "output_platforms", [])
        assert resolve_task_tier(None) == "S"  # 空配置兜底 douyin → S


class TestStoryboardBudget:
    """W4：分镜提示词档位化在位守卫（对齐 design 2026-09-03 §五 分镜层规则）。"""

    def test_s_budget_numbers_injected_from_profile(self):
        from app.services.critic import storyboard_budget_block
        blk = storyboard_budget_block("S")
        p = TIER_PROFILES["S"]
        assert f"整片 {p['dur_min']}–{p['dur_max']} 秒" in blk
        assert f"分镜 {p['shots_min']}–{p['shots_max']} 镜" in blk
        assert f"{p['shot_sec_min']}–{p['shot_sec_max']} 秒" in blk
        assert f"{p['chars_min']}–{p['chars_max']} 字" in blk
        assert f"≤{p['nar_max']} 字" in blk

    def test_s_five_beat_arc_and_rates_present(self):
        from app.services.critic import storyboard_budget_block
        blk = storyboard_budget_block("S")
        for kw in ("五拍弧线", "钩子镜", "人设/背景镜", "意境蓄力镜", "金句镜", "收尾定格镜",
                   "30–50%", "次末镜"):
            assert kw in blk, f"S 分镜预算缺 {kw}"

    def test_l_budget_and_arc_present(self):
        from app.services.critic import storyboard_budget_block
        blk = storyboard_budget_block("L")
        p = TIER_PROFILES["L"]
        assert f"整片 {p['dur_min']}–{p['dur_max']} 秒" in blk
        assert f"分镜 {p['shots_min']}–{p['shots_max']} 镜" in blk
        assert "五拍弧线" in blk and "金句镜" in blk
        assert "≥50%" in blk

    def test_narration_cap_in_full_prompt_matches_profile(self):
        from app.services.critic import storyboard_user_prompt
        for t in ("S", "L"):
            prompt = storyboard_user_prompt("文案示例。", t)
            m = re.search(r"narration ≤\s*(\d+)\s*字", prompt)
            assert m is not None, f"{t} prompt 缺 narration 上限"
            assert int(m.group(1)) == TIER_PROFILES[t]["nar_max"]

    def test_legacy_fixed_shot_budget_retired(self):
        from app.services.critic import storyboard_user_prompt
        prompt = storyboard_user_prompt("文案示例。", "S")
        # 旧 prompt 把镜头数写死"每5秒一镜/≥20 镜/20-24"；现按档注入。
        # 注意：S 预算段含"禁止再按每5秒一镜均分"红线句，故只断言旧的口径短语全退役。
        for legacy in ("每5秒一个分镜", "每5秒一个分镜，总时长约2分钟",
                       "不少于20个分镜", "建议 20-24 个", "总时长约2分钟"):
            assert legacy not in prompt, f"S 分镜 prompt 残留旧写死预算: {legacy}"

    def test_unknown_tier_defaults_s(self):
        from app.services.critic import storyboard_budget_block, storyboard_user_prompt
        assert storyboard_budget_block(None) == storyboard_budget_block("S")
        assert storyboard_budget_block("X") == storyboard_budget_block("S")
        assert storyboard_user_prompt("x", "X") == storyboard_user_prompt("x", "S")
