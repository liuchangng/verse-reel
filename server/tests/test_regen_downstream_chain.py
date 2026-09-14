"""单阶段重跑「连带下游」守护测试（2026-09-14 change-id=regen-downstream-chain）。

事故（代码走查 + 状态推演坐实）：
    ``POST /api/tasks/{id}/regenerate?stage=script`` 旧实现只
    ``enqueue_task(stages=['script'])``，但
      (a) ``enqueue_task`` 会删除该任务**全部** pending/running Job（queue.py:457-459，
          不区分阶段）；
      (b) ``_expand_prereqs`` 只补**上游**前置，不补下游；
      (c) ``_maybe_finalize`` 只在"已存在的 Job"上判定 ``all(done)``（queue.py:1052-1071）。
    三者叠加 ⇒ 下游 Job 已不存在 ⇒ 任务被判 ``pending_review / progress=95``，
    **却没有任何成片**（静默假完成）。而 ``run_stage.script`` 明确写着"文案是全链
    源头，重跑即全链失效"（主动清空 character_ref/image_urls/audio_url/subtitle_url），
    与"只入队 script 一条 Job"自相矛盾 —— 意图与实现不一致。

修复：``_downstream_dependents(stage)`` 取「该阶段 + 全体传递依赖者」中已启用的阶段。
    按**依赖闭包**而非 STAGE_ORDER 位置截断：否则 ``stage=character`` 会连带入队
    tts 并清空其 ``audio_url``，而 tts 只依赖 script ⇒ 用户会看到"只重生成定妆照，
    音频却也没了"。
"""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.api.tasks import regenerate_task
from app.database import Base
from app.models.task import Task
from app.services.queue import (
    STAGE_ORDER,
    _downstream_dependents,
    _enabled_stages,
)
import app.api.tasks as tasks_api


# ---------------------------------------------------------------- #
# 1) 依赖闭包本身
# ---------------------------------------------------------------- #

def test_script_chains_all_enabled_stages():
    """文案是全链源头：script 必须连带全部启用阶段（否则下游 Job 缺失 → 假完成）。"""
    assert _downstream_dependents("script") == list(_enabled_stages())


def test_image_chains_only_render_stages():
    """image 只需连带 video/subtitle；tts 与 publish_copy 不依赖图片，不得被卷入。"""
    enabled = set(_enabled_stages())
    expected = [s for s in STAGE_ORDER if s in {"image", "video", "subtitle"} and s in enabled]
    assert _downstream_dependents("image") == expected
    assert "tts" not in _downstream_dependents("image")
    assert "publish_copy" not in _downstream_dependents("image")


def test_character_does_not_drag_tts_or_publish_copy():
    """定妆照与音频/发布文案无依赖关系 —— 不得连带清空它们的产物。"""
    got = _downstream_dependents("character")
    assert "character" in got and "image" in got and "subtitle" in got
    assert "tts" not in got, "tts 前置是 script，与定妆照无关，连带会误清 audio_url"
    assert "publish_copy" not in got


def test_tts_chains_subtitle_only():
    """tts 的下游只有 subtitle（subtitle 依赖 tts 合成旁白）。"""
    got = _downstream_dependents("tts")
    assert "tts" in got and "subtitle" in got
    assert "image" not in got and "publish_copy" not in got


def test_subtitle_is_leaf():
    """subtitle 是叶子：发布文案依赖 script 而非成片，不得被连带重跑（浪费 LLM 调用）。"""
    assert _downstream_dependents("subtitle") == ["subtitle"]
    assert _downstream_dependents("publish_copy") == ["publish_copy"]
    assert _downstream_dependents("video") == ["video"]


def test_unknown_stage_passthrough():
    """非标准阶段原样返回（调用方自行校验合法性）。"""
    assert _downstream_dependents("not-a-stage") == ["not-a-stage"]


def test_disabled_stage_not_substituted(monkeypatch):
    """请求的阶段**自身未启用**时，不得"换个阶段跑"，也不得把它的依赖者塞进来。

    实测踩到的坑：``enable_agnes_video=False`` 时，video 的依赖者 subtitle 会被
    闭包卷进来 ⇒ ``stages`` 变成 ``['subtitle']``，而用户要的 video 被丢掉
    （旧契约是 ``['video']``，由 ``enqueue_task`` 内的 ``_expand_prereqs`` 裁剪）。
    """
    import app.services.queue as q
    monkeypatch.setattr(q, "_enabled_stages",
                        lambda: [s for s in STAGE_ORDER if s != "image"])
    assert q._downstream_dependents("image") == ["image"]


def test_video_chains_subtitle_when_enabled(monkeypatch):
    """video 启用时：video 是 subtitle 的前置 ⇒ 重跑 video 必须连带 subtitle。"""
    import app.services.queue as q
    monkeypatch.setattr(q, "_enabled_stages", lambda: list(STAGE_ORDER))
    got = q._downstream_dependents("video")
    assert got == ["video", "subtitle"], got


# ---------------------------------------------------------------- #
# 2) 接口层：regenerate 真的入队了下游
# ---------------------------------------------------------------- #

@pytest_asyncio.fixture()
async def regen_env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    calls = []

    async def fake_enqueue(task_id, stages=None, clear_outputs=False, source="unknown"):
        calls.append({"task_id": task_id, "stages": stages,
                      "clear_outputs": clear_outputs, "source": source})
        return len(stages) if stages else 0

    monkeypatch.setattr(tasks_api.queue_service, "enqueue_task", fake_enqueue)
    yield factory, calls
    await engine.dispose()


async def _mk_task(factory, status="failed"):
    async with factory() as db:
        t = Task(poem_id=1, status=status, platform="douyin")
        db.add(t)
        await db.commit()
        return t.id


@pytest.mark.asyncio()
async def test_regenerate_script_enqueues_downstream(regen_env):
    """回归：stage=script 必须连带 image/tts/subtitle 等下游，否则任务假完成无成片。"""
    factory, calls = regen_env
    tid = await _mk_task(factory)

    resp = await regenerate_task(task_id=tid, stage="script", force=False, db=factory())

    assert len(calls) == 1
    stages = calls[0]["stages"]
    assert stages == list(_enabled_stages()), stages
    for need in ("image", "tts", "subtitle"):
        assert need in stages, f"{need} 必须随 script 一并入队"
    assert resp["enqueued_stages"] == stages, "响应需回显真实入队阶段，前端据此展示"
    assert calls[0]["source"] == "regenerate"


@pytest.mark.asyncio()
async def test_regenerate_image_enqueues_subtitle_not_tts(regen_env):
    """stage=image 需连带 subtitle（成片要重建），但不得连带 tts。"""
    factory, calls = regen_env
    tid = await _mk_task(factory)

    await regenerate_task(task_id=tid, stage="image", force=False, db=factory())

    stages = calls[0]["stages"]
    assert "image" in stages and "subtitle" in stages
    assert "tts" not in stages


@pytest.mark.asyncio()
async def test_regenerate_subtitle_stays_single(regen_env):
    """leaf 阶段重跑仍是单阶段（避免无谓的生成与产物清空）。"""
    factory, calls = regen_env
    tid = await _mk_task(factory)

    await regenerate_task(task_id=tid, stage="subtitle", force=False, db=factory())

    assert calls[0]["stages"] == ["subtitle"]
