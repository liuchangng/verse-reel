"""阶段产物失效传播 守护测试（2026-09-09 任务001事故，用户定夺）

事故：重新生成文案只清了图片侧产物，旧音频/旧视频残留——前端按产物推断
状态出现"视频✓图片进行中"的乱序假象，video 产物自检还会复用旧片；video
阶段另有"拿定妆照凑数出片"的静默兜底，视频与分镜彻底脱节。

用户定夺：
- script 重跑 → 下游产物全清（文案是全链源头）；
- video 缺分镜图 → 直接失败（fail-loud），不做定妆照兜底。
"""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database import Base
from app.models.task import Task
from app.models.poem import Poem
from app.services.critic import ScoreResult
from app.services.pipeline import pipeline_engine


@pytest_asyncio.fixture()
async def hygiene_env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as s:
        s.add(Poem(id=1, title="静夜思", author="李白", dynasty="唐",
                   content="床前明月光，疑是地上霜。"))
        await s.commit()

    async def fake_generate_script(db, task, poem, style, keywords):
        script_text = "文案正文「金句」。"
        task.script = script_text
        await db.commit()
        return script_text, ScoreResult(score=8.0, passed=True, feedback="ok")

    async def fake_storyboard(script, tier="S"):
        return [{"time": "0-3s", "description": "月夜", "narration": "床前明月光"}]

    monkeypatch.setattr(pipeline_engine, "_generate_script", fake_generate_script)
    monkeypatch.setattr(pipeline_engine, "_generate_storyboard", fake_storyboard)
    yield factory
    await engine.dispose()


# ---------------------------------------------------------------- #
# script 重跑 → 下游产物全清
# ---------------------------------------------------------------- #

@pytest.mark.asyncio()
async def test_script_rerun_clears_all_downstream(hygiene_env):
    """重跑文案后，分镜/定妆照/分镜图/音频/视频/成片必须全部失效。

    任务001真实事故：只清图片侧 → 旧视频残留 → 前端乱序显示 + 旧片复用。
    """
    factory = hygiene_env
    async with factory() as s:
        s.add(Task(
            id=1, poem_id=1, platform="douyin",
            # 旧文案已存在（_have("script") 需 force 才重跑，这里模拟产物被
            # 旁路清空的场景：script 为空但下游产物残留）
            storyboard='[{"narration": "旧分镜"}]',
            character_ref="https://old/char.png",
            image_urls='["https://old/1.png"]',
            image_score=8,
            audio_url="https://old/audio.mp3",
            video_url="https://old/video.mp4",
            video_duration=30,
            subtitle_url="https://old/final.mp4",
        ))
        await s.commit()

    async with factory() as db:
        await pipeline_engine.run_stage(db, 1, "script")
        task = await db.get(Task, 1)
        assert task.storyboard, "script 阶段应生成新分镜"
        for field in ("character_ref", "image_urls", "image_score", "audio_url",
                      "video_url", "video_duration", "subtitle_url"):
            assert getattr(task, field) is None, f"重跑文案后 {field} 必须被清空"


# ---------------------------------------------------------------- #
# video 缺分镜图 → 失败（无定妆照兜底）
# ---------------------------------------------------------------- #

@pytest.mark.asyncio()
async def test_video_fails_without_storyboard_images(hygiene_env, monkeypatch):
    """分镜图为空时 video 必须失败；即使有定妆照也不得兜底出片。"""
    factory = hygiene_env
    async with factory() as s:
        s.add(Task(
            id=2, poem_id=1, platform="douyin",
            character_ref="https://old/char.png",   # 有定妆照
            image_urls=None,                        # 但分镜图缺失
        ))
        await s.commit()

    async def fail_if_called(*a, **k):
        raise AssertionError("缺分镜图时不得调用视频生成（定妆照兜底已移除）")

    monkeypatch.setattr(pipeline_engine, "_generate_video", fail_if_called)

    async with factory() as db:
        with pytest.raises(RuntimeError, match="缺少前置 image"):
            await pipeline_engine.run_stage(db, 2, "video")


@pytest.mark.asyncio()
async def test_video_runs_with_storyboard_images(hygiene_env, monkeypatch):
    """分镜图齐全时 video 正常出片（去兜底不误伤正常链路）"""
    factory = hygiene_env
    async with factory() as s:
        s.add(Task(id=3, poem_id=1, platform="douyin",
                   image_urls='["https://img/1.png", "https://img/2.png"]'))
        await s.commit()

    async def fake_generate_video(task, image_urls, platform_cfg, style):
        assert len(image_urls) == 2, "必须使用全部分镜图"
        return "https://video/new.mp4"

    monkeypatch.setattr(pipeline_engine, "_generate_video", fake_generate_video)

    async with factory() as db:
        await pipeline_engine.run_stage(db, 3, "video")
        task = await db.get(Task, 3)
        assert task.video_url == "https://video/new.mp4"
