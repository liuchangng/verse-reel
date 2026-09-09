"""热点兜底不固化守护测试（2026-09-09 问题 3-②）

背景：网络全断时 fetch_hotspots 落到静态 FALLBACK（"中秋月圆夜"等写死数据），
旧版 save_hotspots 无条件全局 DELETE + 插入 → 静态假数据固化写库，
UI 永远展示同样的内容。修复后：fallback 条目打标、save_hotspots 跳过，
且 DELETE 改为按平台，未刷新平台的旧真实数据保留。
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import hotspot
from app.models.hotspot import Hotspot


class FakeResult:
    def scalars(self):
        class _S:
            def all(self):
                return []
        return _S()


class FakeDb:
    """记录 execute 语句/参数与 add 的对象，供断言。"""

    def __init__(self):
        self.executed = []   # (sql_text, params)
        self.added = []

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        return FakeResult()

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


def _real_item(title):
    return {"title": title, "hot": 10, "url": "https://x", "_is_fallback": False}


def _fallback_item(title):
    return {"title": title, "hot": 10, "url": "", "_is_fallback": True}


@pytest.mark.asyncio
async def test_save_skips_fallback_items():
    """混合场景：fallback 条目不写库，真实条目按平台写入。"""
    db = FakeDb()
    await hotspot.hotspot_service.save_hotspots(db, {
        "weibo": [_real_item("真实热搜A"), _real_item("真实热搜B")],
        "douyin": [_fallback_item("中秋月圆夜")],   # 全兜底 → 整平台跳过
    })
    platforms_deleted = [params["p"] for sql, params in db.executed if params]
    assert platforms_deleted == ["weibo"], "只应清空有真实数据的平台"
    assert all(h.platform == "weibo" for h in db.added), "只应写入 weibo 的真实条目"
    assert len(db.added) == 2


@pytest.mark.asyncio
async def test_save_all_fallback_no_writes():
    """全兜底场景：不执行任何 DELETE/INSERT，库内数据原样保留。"""
    db = FakeDb()
    await hotspot.hotspot_service.save_hotspots(db, {
        "weibo": [_fallback_item("中秋月圆夜")],
        "baidu": [_fallback_item("古诗词里的乡愁")],
    })
    assert db.executed == [] and db.added == []


@pytest.mark.asyncio
async def test_fetch_marks_fallback_items(monkeypatch):
    """NewsNow 与直连全失败时，fetch 结果应带 _is_fallback 标记。"""

    async def newsnow_fail(*a, **k):
        raise RuntimeError("newsnow down")

    class FailClient:
        async def get(self, *a, **k):
            raise RuntimeError("direct down")

    monkeypatch.setattr(hotspot.hotspot_service, "_fetch_newsnow", newsnow_fail)
    monkeypatch.setattr(hotspot.hotspot_service, "client", FailClient())
    monkeypatch.setattr(hotspot.settings, "newsnow_base_url", "http://127.0.0.1:1")

    res = await hotspot.hotspot_service.fetch_hotspots(platforms=["weibo"], limit=3)
    assert res["weibo"], "兜底也应返回条目（进程内使用）"
    assert all(it.get("_is_fallback") for it in res["weibo"]), "全部条目应打上兜底标记"
