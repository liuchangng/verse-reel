"""platform_outputs API 层守护测试（change: multi-platform-final-videos-platform-outputs）。

req:
- REQ-M5：GET /tasks/{id} 的 platform_outputs 由裸 JSON 字符串改为
  契约层归一化的 {platform: entry} 对象；旧 {platform: "url"} 自动归一；非法 JSON 降级 {}。
- REQ-M7：POST /tasks/{id}/publish 按平台取对应成片（entry.url）；
  请求平台在 platform_outputs 中无成功 entry 时 400，不静默回退主平台。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from fastapi.testclient import TestClient

from app.database import Base, get_db
from app.main import app
from app.models.task import Task
from app.models.poem import Poem


@pytest_asyncio.fixture()
async def env():
    """内存 SQLite + 覆写 get_db 依赖，供 TestClient 走真实路由。"""
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    yield factory
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest_asyncio.fixture()
async def env_with_data(env, monkeypatch):
    """预置 poem + 完成态任务（done, video_url 已设）；同时放行鉴权。"""
    # /api 路由挂 require_token（Bearer <app_token>）：测试里置一个固定 token 放行
    from app.config import settings
    monkeypatch.setattr(settings, "app_token", "test-token")
    async with env() as s:
        s.add(Poem(id=1, title="定风波", author="苏轼", dynasty="宋",
                   content="莫听穿林打叶声，何妨吟啸且徐行。"))
        s.add(Task(id=1, poem_id=1, platform="douyin", platforms="[]",
                   status="done", script="文案正文",
                   video_url="http://x/final_primary.mp4"))
        await s.commit()
    yield env


def _client():
    """带 Bearer 鉴权头的 TestClient（/api 挂 require_token，token 由 env_with_data 置位）。"""
    return TestClient(app, headers={"Authorization": "Bearer test-token"})


# ---------------------------------------------------------------- #
# REQ-M5: GET /tasks/{id} 结构化返回 platform_outputs
# ---------------------------------------------------------------- #

@pytest.mark.asyncio()
async def test_get_task_platform_outputs_is_structured(env_with_data):
    """DB 中 platform_outputs 为结构化 JSON 时，API 返回 object（非裸字符串）。"""
    raw = json.dumps({
        "douyin": {"url": "http://x/final_douyin.mp4", "ratio": "9:16",
                   "duration": 32, "status": "ok"},
        "bilibili": {"ratio": "16:9", "status": "failed", "error": "烧录失败"},
    }, ensure_ascii=False)
    async with env_with_data() as s:
        t = await s.get(Task, 1)
        t.platform_outputs = raw
        await s.commit()

    c = _client()
    resp = c.get("/api/tasks/1")
    assert resp.status_code == 200
    po = resp.json()["platform_outputs"]
    assert isinstance(po, dict), "REQ-M5: platform_outputs 必须是 object，不再是裸字符串"
    assert po["douyin"]["url"].endswith("final_douyin.mp4")
    assert po["douyin"]["ratio"] == "9:16"
    assert po["bilibili"]["status"] == "failed"
    assert "url" not in po["bilibili"]


@pytest.mark.asyncio()
async def test_get_task_legacy_url_string_normalized(env_with_data):
    """旧 {platform: "url"} 弱契约数据经 API 解析后自动归一为成功 entry。"""
    raw = json.dumps({"douyin": "http://x/final_douyin.mp4",
                      "kuaishou": "http://x/final_kuaishou.mp4"},
                     ensure_ascii=False)
    async with env_with_data() as s:
        t = await s.get(Task, 1)
        t.platform_outputs = raw
        await s.commit()

    c = _client()
    po = c.get("/api/tasks/1").json()["platform_outputs"]
    assert po["douyin"]["url"] == "http://x/final_douyin.mp4"
    assert po["douyin"]["status"] == "ok"


@pytest.mark.asyncio()
async def test_get_task_invalid_json_degrades_to_empty(env_with_data):
    """DB 中非法 JSON 不得导致 500，降级为 {}（REQ-M5 边界）。"""
    async with env_with_data() as s:
        t = await s.get(Task, 1)
        t.platform_outputs = "{not-valid-json"
        await s.commit()

    c = _client()
    resp = c.get("/api/tasks/1")
    assert resp.status_code == 200
    assert resp.json()["platform_outputs"] == {}


@pytest.mark.asyncio()
async def test_get_task_no_outputs_returns_empty_object(env_with_data):
    """单平台任务 platform_outputs 为 None → API 返回空 dict（前端兜底 video_url）。"""
    c = _client()
    po = c.get("/api/tasks/1").json()["platform_outputs"]
    assert po == {}


# ---------------------------------------------------------------- #
# REQ-M7: 发布入口按平台取对应成片
# ---------------------------------------------------------------- #

@pytest.mark.asyncio()
async def test_publish_platform_failed_returns_400(env_with_data, monkeypatch):
    """请求了某平台但 entry 失败 → 400 明确原因（不静默回退主平台）。"""
    raw = json.dumps({
        "douyin": {"url": "http://x/final_douyin.mp4", "ratio": "9:16", "status": "ok"},
        "bilibili": {"ratio": "16:9", "status": "failed", "error": "烧录失败"},
    }, ensure_ascii=False)
    async with env_with_data() as s:
        t = await s.get(Task, 1)
        t.platform_outputs = raw
        await s.commit()

    from app.services import publisher as pub_mod
    captured: list = []

    async def fake_publish_multiple(video_path, title, description, tags, platforms):
        captured.append((list(platforms), video_path))
        class _R:
            platform = platforms[0]
            success = True
            message = "ok"
        return [_R() for p in platforms]

    monkeypatch.setattr(pub_mod.publisher_service, "publish_to_multiple",
                       fake_publish_multiple)

    c = _client()
    resp = c.post("/api/tasks/1/publish", params={"platforms": "bilibili", "confirm": "YES"})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "bilibili" in detail
    assert "烧录失败" in detail
    assert captured == [], "失败平台不得触发发布"


@pytest.mark.asyncio()
async def test_publish_missing_platform_returns_400(env_with_data, monkeypatch):
    """请求了 platform_outputs 中根本不存在的平台 → 400。"""
    raw = json.dumps({"douyin": {"url": "http://x/final_douyin.mp4", "status": "ok"}},
                     ensure_ascii=False)
    async with env_with_data() as s:
        t = await s.get(Task, 1)
        t.platform_outputs = raw
        await s.commit()

    from app.services import publisher as pub_mod

    async def fake_publish_multiple(video_path, title, description, tags, platforms):
        return []

    monkeypatch.setattr(pub_mod.publisher_service, "publish_to_multiple",
                       fake_publish_multiple)

    c = _client()
    resp = c.post("/api/tasks/1/publish", params={"platforms": "youtube", "confirm": "YES"})
    assert resp.status_code == 400
    assert "youtube" in resp.json()["detail"]


@pytest.mark.asyncio()
async def test_publish_ok_platform_uses_own_url(env_with_data, monkeypatch):
    """成功平台使用 platform_outputs 中自己的成片 URL（而非主平台 video_url）。"""
    raw = json.dumps({
        "douyin": {"url": "http://x/outputs/task_1/final_douyin.mp4",
                   "ratio": "9:16", "status": "ok"},
        "bilibili": {"url": "http://x/outputs/task_1/final_bilibili.mp4",
                     "ratio": "16:9", "status": "ok"},
    }, ensure_ascii=False)
    async with env_with_data() as s:
        t = await s.get(Task, 1)
        t.platform_outputs = raw
        await s.commit()

    from app.services import publisher as pub_mod
    captured: list = []

    async def fake_publish_multiple(video_path, title, description, tags, platforms):
        captured.append((list(platforms), video_path))
        class _R:
            platform = platforms[0]
            success = True
            message = "ok"
        return [_R() for p in platforms]

    monkeypatch.setattr(pub_mod.publisher_service, "publish_to_multiple",
                       fake_publish_multiple)

    c = _client()
    # FastAPI Query(list[str]) 需重复参数表达多值（非逗号串）
    resp = c.post("/api/tasks/1/publish",
                  params=[("platforms", "douyin"), ("platforms", "bilibili"), ("confirm", "YES")])
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert {r["platform"] for r in results} == {"douyin", "bilibili"}
    # 核心断言：每平台拿到自己的成片 URL（REQ-M7）
    url_map = {p: url for (plats, url) in captured for p in plats}
    assert url_map["douyin"] == "http://x/outputs/task_1/final_douyin.mp4"
    assert url_map["bilibili"] == "http://x/outputs/task_1/final_bilibili.mp4"
