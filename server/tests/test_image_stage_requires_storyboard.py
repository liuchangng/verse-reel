"""image 阶段「档位无分镜必须 fail-loud」守护测试。

change-id: image-stage-storyboard-required（2026-09-14）

事故（与 tts 那次同构，只是换了阶段）：
    ``run_stage.image`` 逐档遍历时，某档 ``storyboard`` 为空则
    ``logger.warning + continue`` —— 该档被静默跳过、Job 仍以 **done** 收尾，
    但 ``task.image_urls`` / ``task.image_local_paths`` 始终为空。而
    ``queue._task_produced()['image']`` 要求「CDN URL + 本地落盘都非空」
    ⇒ 恒判 "image 未产出" ⇒ ``subtitle``（2026-09-14 起前置含 image，见
    change-id=subtitle-image-prereq）的 ``_deps_satisfied`` 永不满足
    ⇒ ``queue._heal_prereqs`` 每轮轮询补建一条 image Job ⇒ **无界自愈死循环**。
    旧防护只覆盖「前置已 failed」，而空转 Job 是 done，故兜不住。

修复：档位无分镜时直接抛错（并在 task 上留具体原因）⇒ Job 落 failed ⇒
下一次自愈即级联失败、任务干净落终态。

反例守护：分镜齐备时必须正常推进，不得误报（防"修过头"）。
"""
import json

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database import Base
from app.models.task import Task
from app.models.poem import Poem
from app.services.pipeline import pipeline_engine


SHOT = {"time": "0-3s", "description": "月夜", "narration": "床前明月光"}


@pytest_asyncio.fixture()
async def img_env():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Poem(id=1, title="静夜思", author="李白", dynasty="唐",
                   content="床前明月光，疑是地上霜。"))
        await s.commit()
    yield factory
    await engine.dispose()


async def _mk_task(factory, **kw):
    async with factory() as s:
        s.add(Task(id=1, poem_id=1, platform="douyin", **kw))
        await s.commit()


# ---------------------------------------------------------------- #
# 1) 单档无分镜 → 必须抛错，且不得调用生图（旧实现静默 done）
# ---------------------------------------------------------------- #

@pytest.mark.asyncio()
async def test_image_raises_when_tier_has_no_storyboard(img_env, monkeypatch):
    factory = img_env
    await _mk_task(factory, script="文案", storyboards_json=json.dumps({
        "S": {"script": "快档", "storyboard": []},      # ← 无分镜
    }, ensure_ascii=False))

    called: list[int] = []

    async def boom(*a, **k):
        called.append(1)
        raise AssertionError("无分镜的档位不应发起生图")

    monkeypatch.setattr(pipeline_engine, "_generate_images", boom)

    async with factory() as s:
        with pytest.raises(RuntimeError) as ei:
            await pipeline_engine.run_stage(s, 1, "image")
    assert "缺少分镜" in str(ei.value) and "S" in str(ei.value), str(ei.value)
    assert called == [], "不得空转生图"

    async with factory() as s:
        t = await s.get(Task, 1)
        assert t.status == "failed"
        assert "image 缺少分镜" in (t.error_message or "")


# ---------------------------------------------------------------- #
# 2) 多档中一档缺分镜 → 抛错并点名缺失档位
# ---------------------------------------------------------------- #

@pytest.mark.asyncio()
async def test_image_raises_and_names_missing_tier(img_env, monkeypatch):
    factory = img_env
    await _mk_task(factory, script="文案", storyboards_json=json.dumps({
        "S": {"script": "快档", "storyboard": [SHOT]},
        "L": {"script": "深档", "storyboard": []},      # ← 缺失
    }, ensure_ascii=False))

    async def boom(*a, **k):
        raise AssertionError("不得空转生图")

    monkeypatch.setattr(pipeline_engine, "_generate_images", boom)

    async with factory() as s:
        with pytest.raises(RuntimeError) as ei:
            await pipeline_engine.run_stage(s, 1, "image")
    assert "档位 L" in str(ei.value), str(ei.value)


# ---------------------------------------------------------------- #
# 3) 反例守护：分镜齐备时必须正常推进（不得误报）
# ---------------------------------------------------------------- #

@pytest.mark.asyncio()
async def test_image_proceeds_when_storyboard_present(img_env, monkeypatch):
    factory = img_env
    await _mk_task(factory, script="文案", storyboards_json=json.dumps({
        "S": {"script": "快档", "storyboard": [SHOT]},
    }, ensure_ascii=False))

    async def fake_images(task, storyboard, db=None, character_ref=None,
                          style=None, character_description=None):
        return ["http://cdn/1.png"]

    async def fake_persist(task, urls, tier=""):
        return ["/local/1.png"]

    monkeypatch.setattr(pipeline_engine, "_generate_images", fake_images)
    monkeypatch.setattr(pipeline_engine, "_persist_images_local", fake_persist)

    async with factory() as s:
        await pipeline_engine.run_stage(s, 1, "image")   # 不应抛错

    async with factory() as s:
        t = await s.get(Task, 1)
        assert t.status != "failed"
        assert json.loads(t.image_urls) == ["http://cdn/1.png"]
