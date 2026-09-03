"""推荐诗词服务单元测试（mock LLM + 独立内存库）"""
import sys
import os
import pytest
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import hotspot


class TestRecommendPoems:
    """recommend_poems：倒排初筛 + LLM 精选"""

    def test_recommend_poems_exists(self):
        """hotspot_service 已有 recommend_poems 方法"""
        assert hasattr(hotspot.hotspot_service, "recommend_poems")

    @pytest.mark.asyncio
    async def test_returns_list_with_match_reason(self, monkeypatch):
        """mock 倒排查询 + LLM，返回带 match_reason 的诗词列表"""
        fake_candidates = [
            {"id": 1, "title": "静夜思", "author": "李白", "dynasty": "唐",
             "content_preview": "床前明月光疑是地上霜"},
            {"id": 2, "title": "月下独酌", "author": "李白", "dynasty": "唐",
             "content_preview": "花间一壶酒"},
        ]

        # mock 一个 db，使其 db.execute 能返回 fake candidates（Poem 详情）
        class FakeResult:
            def scalars(self):
                class _S:
                    def all(self):
                        return fake_poems
                return _S()

        class FakeDb:
            async def execute(self, stmt):
                return FakeResult()

        fake_poems = [
            type("P", (), {"id": 1, "title": "静夜思", "author": "李白",
                           "dynasty": "唐", "content": "床前明月光疑是地上霜"})(),
        ]

        # monkeypatch 规则初筛（DB 查询）与 LLM 精选
        async def fake_rule(db, keywords, limit=20):
            # 返回候选 id 列表
            return [1]

        async def fake_llm(candidates, hotspot):
            # 返回精选的 poem 字典列表（含 match_reason）
            return [
                {"id": c["id"], "title": c["title"], "author": c["author"],
                 "dynasty": c["dynasty"], "content_preview": c["content_preview"],
                 "match_reason": "主题词：明月"}
                for c in candidates
            ]

        monkeypatch.setattr(hotspot.hotspot_service, "_rule_candidates", fake_rule)
        monkeypatch.setattr(hotspot.hotspot_service, "_llm_select", fake_llm)
        # #20260903: 兜底会调 _get_prestige_table 拿作者权威；mock 一次返回空 dict
        async def fake_prest(db):
            return {}
        monkeypatch.setattr(hotspot, "_get_prestige_table", fake_prest)

        hotspot_item = {"title": "中秋节思念家乡", "hot": 123}
        result = await hotspot.hotspot_service.recommend_poems(FakeDb(), hotspot_item)

        assert isinstance(result, list)
        assert len(result) >= 1
        assert "match_reason" in result[0]
        assert "id" in result[0]

    @pytest.mark.asyncio
    async def test_llm_failure_fallback_to_rules(self, monkeypatch):
        """LLM 精选失败 → 兜底返回规则 Top-3"""
        async def fake_rule(db, keywords, limit=20):
            return [1, 2]

        async def fake_llm_fail(candidates, hotspot):
            return []  # LLM 返回空

        class FakeDb:
            async def execute(self, stmt):
                class _P:  # 占位 Poem 对象
                    id, title, author, dynasty, content = 1, "静夜思", "李白", "唐", "床前明月光"
                class _S:
                    def all(self):
                        return [_P()]
                return type("R", (), {"scalars": lambda self: _S()})()

        monkeypatch.setattr(hotspot.hotspot_service, "_rule_candidates", fake_rule)
        monkeypatch.setattr(hotspot.hotspot_service, "_llm_select", fake_llm_fail)
        async def fake_prest2(db):
            return {"苏轼": 7866, "卢青山": 50}
        monkeypatch.setattr(hotspot, "_get_prestige_table", fake_prest2)

        hotspot_item = {"title": "秋天思念家乡", "hot": 456}
        result = await hotspot.hotspot_service.recommend_poems(FakeDb(), hotspot_item)

        # 兜底：返回规则 Top-3 且有 match_reason
        assert len(result) >= 1
        assert result[0]["match_reason"].startswith("主题词命中")
