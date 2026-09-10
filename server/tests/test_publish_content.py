"""Q3 守护测试：按需 LLM 平台发布文案生成（publisher.generate_platform_copy）

覆盖（2026-09-10 Q3「B 方案 LLM + 按平台分块 + 查看时按需生成」）：
- 各平台字段规范表齐全（抖音/小红书/快手）
- JSON 安全解析降级（markdown 包裹 / 前后杂质 / 裸控制字符 / 彻底坏 → {}）
- 缓存键契约（同 script+platforms 同键；script 变键变；platforms 无序同键）
- 无 script → 直接规则版（generated=False），只含已定义规范平台
- 未知平台被过滤
- LLM 成功路径：mock agnes_client.generate_text，按平台覆盖为 generated=True
- LLM 失败路径：抛异常 → 全平台回退规则版（generated=False），不抛错
"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services.publisher import (
    PLATFORM_COPY_SPECS,
    PublisherService,
    publisher_service,
)


@pytest.fixture
def svc():
    """每个用例独立缓存的实例（避免跨用例污染进程级缓存）。"""
    s = PublisherService()
    s._copy_cache = {}
    return s


# ---------- 平台规范表 ----------
def test_specs_covers_three_platforms():
    assert set(PLATFORM_COPY_SPECS) == {"douyin", "xiaohongshu", "kuaishou"}
    for p, s in PLATFORM_COPY_SPECS.items():
        assert s["title_max"] > s["title_min"] > 0
        assert s["tag_count"]


# ---------- JSON 解析降级 ----------
def test_parse_copy_json_clean():
    raw = '{"douyin": {"title": "t", "description": "d", "tags": ["a", "b"]}}'
    out = PublisherService._parse_copy_json(raw)
    assert out["douyin"]["tags"] == ["a", "b"]


def test_parse_copy_json_with_markdown_block():
    raw = "好的，结果是：\n```json\n{\"douyin\": {\"title\": \"t\"}}\n```\n完毕"
    out = PublisherService._parse_copy_json(raw)
    assert out["douyin"]["title"] == "t"


def test_parse_copy_json_with_surrounding_garbage():
    raw = "prefix noise {\"xiaohongshu\": {\"title\": \"x\"}} trailing noise"
    out = PublisherService._parse_copy_json(raw)
    assert out["xiaohongshu"]["title"] == "x"


def test_parse_copy_json_bare_control_chars_sanitized():
    # JSON 字符串值内混入裸换行/制表 → 被清洗成空格，仍可解析
    raw = '{"kuaishou": {"title": "a\\n   b", "tags": ["x\\ty"]}}'
    out = PublisherService._parse_copy_json(raw)
    assert "kuaishou" in out


def test_parse_copy_json_total_garbage_returns_empty():
    assert PublisherService._parse_copy_json("这不是 JSON 也没有大括号") == {}
    assert PublisherService._parse_copy_json("") == {}


# ---------- 缓存键契约 ----------
def test_cache_key_stable_and_order_independent():
    assert (
        publisher_service._copy_cache_key(5, "文案A", ["douyin", "kuaishou"])
        == publisher_service._copy_cache_key(5, "文案A", ["kuaishou", "douyin"])
    )
    # script 变 → 键变
    assert (
        publisher_service._copy_cache_key(5, "文案A", ["douyin"])
        != publisher_service._copy_cache_key(5, "文案B", ["douyin"])
    )
    # task_id 变 → 键变
    assert (
        publisher_service._copy_cache_key(5, "文案A", ["douyin"])
        != publisher_service._copy_cache_key(6, "文案A", ["douyin"])
    )


# ---------- 无 script 直接规则版 ----------
@pytest.mark.asyncio
async def test_no_script_returns_rule_version(svc):
    out = await svc.generate_platform_copy(
        task_id=1, script="", poem_title="定风波", platforms=["douyin", "kuaishou"]
    )
    assert set(out) == {"douyin", "kuaishou"}
    for p in out.values():
        assert p["generated"] is False
        assert p["title"]


# ---------- 未知平台被过滤 ----------
@pytest.mark.asyncio
async def test_unknown_platforms_filtered(svc):
    out = await svc.generate_platform_copy(
        task_id=1, script="", poem_title="定风波", platforms=["douyin", "weibo_unknown"]
    )
    assert set(out) == {"douyin"}


# ---------- LLM 成功路径（mock） ----------
@pytest.mark.asyncio
async def test_llm_success_overrides_by_platform(svc):
    llm_out = {
        "douyin": {"title": "🔥苏轼一生最豁达的词", "description": "风雨兼程…", "tags": ["古诗词", "苏轼", "豁达"]},
        "kuaishou": {"title": "你以为是名句，其实有故事", "description": "白话直给…", "tags": ["古诗词"]},
    }
    with patch("app.services.publisher.agnes_client") as fake:
        fake.generate_text = AsyncMock(return_value=json.dumps(llm_out, ensure_ascii=False))
        out = await svc.generate_platform_copy(
            task_id=7, script="一段文案…", poem_title="定风波",
            author="苏轼", dynasty="宋", platforms=["douyin", "kuaishou"],
        )
    assert out["douyin"]["generated"] is True
    assert out["douyin"]["title"].startswith("🔥")
    assert out["kuaishou"]["tags"] == ["古诗词"]
    # LLM 没给的第三个平台不在请求里，无需断言


# ---------- LLM 只覆盖部分平台（缺 douyin 键 → douyin 回退规则版） ----------
@pytest.mark.asyncio
async def test_llm_missing_platform_falls_back_rule(svc):
    llm_out = {"kuaishou": {"title": "快手标题", "description": "d", "tags": ["a"]}}
    with patch("app.services.publisher.agnes_client") as fake:
        fake.generate_text = AsyncMock(return_value=json.dumps(llm_out, ensure_ascii=False))
        out = await svc.generate_platform_copy(
            task_id=8, script="一段文案…", poem_title="定风波",
            platforms=["douyin", "kuaishou"],
        )
    assert out["kuaishou"]["generated"] is True
    assert out["douyin"]["generated"] is False  # LLM 没出 douyin，回退规则版


# ---------- LLM 抛异常 → 全回退规则版，不炸 ----------
@pytest.mark.asyncio
async def test_llm_exception_falls_back(svc):
    with patch("app.services.publisher.agnes_client") as fake:
        fake.generate_text = AsyncMock(side_effect=RuntimeError("upstream down"))
        out = await svc.generate_platform_copy(
            task_id=9, script="一段文案…", poem_title="定风波",
            platforms=["douyin", "xiaohongshu"],
        )
    assert set(out) == {"douyin", "xiaohongshu"}
    for p in out.values():
        assert p["generated"] is False


# ---------- 缓存命中不重复调 LLM ----------
@pytest.mark.asyncio
async def test_cache_hit_skips_llm(svc):
    llm_out = {"douyin": {"title": "t", "description": "d", "tags": ["a"]}}
    calls = []
    async def fake_gen(messages, **kw):
        calls.append(1)
        return json.dumps(llm_out, ensure_ascii=False)

    with patch("app.services.publisher.agnes_client") as fake:
        fake.generate_text = fake_gen
        out1 = await svc.generate_platform_copy(
            task_id=10, script="文案", poem_title="定风波", platforms=["douyin"]
        )
        out2 = await svc.generate_platform_copy(
            task_id=10, script="文案", poem_title="定风波", platforms=["douyin"]
        )
    assert out1["douyin"]["generated"] is True
    assert out2 is out1 or out2 == out1
    assert len(calls) == 1  # 第二次走缓存，LLM 只调一次
