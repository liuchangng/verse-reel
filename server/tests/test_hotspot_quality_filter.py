"""热点质量过滤守护测试（2026-09-09 问题 4a）

背景：数据源是流量榜（夸大/标题党/舆论引导），"女孩去邻居家吃饭惨遭夫妻分尸"
"iPhone18系列"这类选题与诗词创作完全无关。抓取后两层过滤：
规则层（词面，确定性零成本）+ LLM 批筛层（一次调用，失败降级为规则存活项）。
"""
import pytest

import app.services.hotspot as hmod
from app.services.hotspot import (
    HotspotService,
    _rule_filter_mask,
    _parse_llm_filter_reply,
    _JUNK_TITLE_PATTERNS,
)


def test_rule_filter_kills_crime_and_product_titles():
    """凶案/产品类标题必须被规则层杀掉。"""
    titles = [
        "女孩去邻居家吃饭惨遭夫妻分尸",   # 凶案
        "iPhone18系列正式开售",           # 产品（含英文，lower 后命中）
        "梅姨在广州摆摊卖切块芒果",       # 中性社会新闻，规则层不杀（交 LLM 判）
        "中秋国庆火车票今日开售",         # "开售"命中 → 杀（商业味浓）
        "月到中秋分外明：那些写月亮的诗词",  # 正常选题
    ]
    mask = _rule_filter_mask(titles)
    assert mask == [False, False, True, False, True]


def test_rule_filter_patterns_nonempty_and_case_insensitive():
    """垃圾词表非空；英文模式大小写不敏感。"""
    assert len(_JUNK_TITLE_PATTERNS) >= 30
    assert _rule_filter_mask(["Huawei 发布会"]) == [False]


def test_parse_llm_filter_reply_valid_and_garbage():
    """合法 JSON 数组解析出行号；乱码/越界安全处理。"""
    assert _parse_llm_filter_reply("[0,3,7]", 10) == [0, 3, 7]
    assert _parse_llm_filter_reply('前置说明 [1, 2] 后缀', 5) == [1, 2]
    assert _parse_llm_filter_reply("我保留 0 3 7", 10) is None       # 无数组 → None
    assert _parse_llm_filter_reply("[0,3,99]", 5) == [0, 3]          # 越界剔除
    assert _parse_llm_filter_reply("[0,'x']", 5) == [0]              # 非整数剔除
    assert _parse_llm_filter_reply("", 5) is None


@pytest.mark.asyncio()
async def test_filter_quality_llm_batch_and_fallback(monkeypatch):
    """LLM 批筛正常工作；LLM 失败时降级保留规则存活项；兜底条目不受影响。"""
    svc = HotspotService.__new__(HotspotService)  # 不触网络，只测过滤逻辑

    results = {
        "weibo": [
            {"title": "女孩去邻居家吃饭惨遭夫妻分尸", "hot": 9, "_is_fallback": False},
            {"title": "今夜月圆人团圆", "hot": 8, "_is_fallback": False},
            {"title": "iPhone18系列开售", "hot": 7, "_is_fallback": False},
            {"title": "静态兜底示例", "hot": 1, "_is_fallback": True},
        ],
    }

    # 场景1：LLM 正常，保留行号 1（"今夜月圆人团圆"）
    async def _ok_llm(messages, **kw):
        return "[1]"

    monkeypatch.setattr(hmod.agnes_client, "generate_text", _ok_llm)
    out = await svc._filter_quality(results)
    assert [it["title"] for it in out["weibo"]] == ["今夜月圆人团圆", "静态兜底示例"]

    # 场景2：LLM 挂了 → 降级保留规则存活项
    async def _boom_llm(messages, **kw):
        raise ConnectionError("network down")

    monkeypatch.setattr(hmod.agnes_client, "generate_text", _boom_llm)
    out = await svc._filter_quality(results)
    titles = [it["title"] for it in out["weibo"]]
    assert "女孩去邻居家吃饭惨遭夫妻分尸" not in titles   # 规则层照杀
    assert "iPhone18系列开售" not in titles
    assert "今夜月圆人团圆" in titles                    # LLM 不可用则保留
    assert "静态兜底示例" in titles                      # 兜底标记原样保留


@pytest.mark.asyncio()
async def test_filter_quality_empty_and_all_fallback():
    """全兜底/空输入直接原样返回，不触发 LLM 调用。"""
    svc = HotspotService.__new__(HotspotService)
    only_fallback = {"weibo": [{"title": "兜底", "hot": 1, "_is_fallback": True}]}
    out = await svc._filter_quality(only_fallback)
    assert out == only_fallback
    assert await svc._filter_quality({}) == {}
