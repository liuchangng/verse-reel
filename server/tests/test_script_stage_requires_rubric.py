"""script 阶段「文案评分未达标必须 fail-loud」守护测试。

change-id: script-rubric-fail-loud（2026-09-14）

事故（实测）：
    ``run_stage.script`` 在文案评分未达标时只把 ``task.status`` 置 ``failed``
    然后 **return** ⇒ ``_run_job`` 视作成功、Job 以 **done** 收尾。同一任务内
    "task=failed / Job=done" 自相矛盾，且下游 ``_heal_prereqs`` 仍会按
    ``_task_produced()['script']``（只看文案非空）判定前置已满足，继续补建
    image/tts/subtitle Job 白跑 —— 实测由 subtitle 触发的「补建缺失前置 ['tts']」
    达 1300+ 次，是本轮无界自愈死循环的**上游源头**。

修复语义（三条）：
    1. script 阶段的「完成」= 文案正文 **且** 分镜就绪（分镜只在评分通过时落库）；
    2. 文案已存在但分镜缺失 ⇒ 上一轮评分未达标，属确定性失败：立即抛错、
       **不重复烧 LLM**；
    3. 评分未达标 ⇒ 抛错而非 return，让 Job 落 failed 后由
       ``queue._maybe_finalize`` 落任务终态并级联失败其余 pending Job。

与 ``test_tts_stage_requires_storyboard.py``（tts 侧兜底）配套：那个守的是
下游不得静默空转，本文件守的是**上游根因**不得静默假完成。
"""
import json

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database import Base
from app.models.task import Task
from app.models.poem import Poem
from app.services.critic import ScoreResult
from app.services.pipeline import pipeline_engine


SHOT = {"time": "0-3s", "description": "月夜", "narration": "床前明月光"}
SB_JSON = json.dumps({"S": {"script": "文案", "storyboard": [SHOT]}}, ensure_ascii=False)


@pytest_asyncio.fixture()
async def script_env():
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
    """建一个 id=1 的任务（Poem 由 fixture 预置）。"""
    async with factory() as s:
        s.add(Task(id=1, poem_id=1, platform="douyin", **kw))
        await s.commit()


# ---------------------------------------------------------------- #
# 1) 评分未达标 → 必须抛错（旧实现 return ⇒ Job done）
# ---------------------------------------------------------------- #

@pytest.mark.asyncio()
async def test_script_raises_when_rubric_not_passed(script_env, monkeypatch):
    factory = script_env
    await _mk_task(factory)

    async def fake_gen(db, task, poem, style="人生感悟", keywords=None):
        # 还原 _generate_script 的真实落库形态：文案恒落库、分镜不落库
        task.script = "未达标文案正文"
        task.script_score = 4.0
        await db.commit()
        return task.script, ScoreResult(score=4.0, feedback="字数不足且缺金句", passed=False)

    monkeypatch.setattr(pipeline_engine, "_generate_script", fake_gen)

    async with factory() as s:
        with pytest.raises(RuntimeError) as ei:
            await pipeline_engine.run_stage(s, 1, "script")
    assert "文案评分未达标" in str(ei.value), str(ei.value)

    async with factory() as s:
        t = await s.get(Task, 1)
        assert t.status == "failed"
        assert "文案评分未达标" in (t.error_message or "")


# ---------------------------------------------------------------- #
# 2) 文案在、分镜缺（= 上次评分未达标）→ 抛错且不得再调 LLM
# ---------------------------------------------------------------- #

@pytest.mark.asyncio()
async def test_script_raises_without_calling_llm_when_storyboard_missing(script_env, monkeypatch):
    factory = script_env
    await _mk_task(factory, script="旧文案", script_score=4.0)   # 有文案、无分镜

    called: list[int] = []

    async def boom(*a, **k):
        called.append(1)
        raise AssertionError("不应重复调用 LLM：文案已在而分镜缺失属确定性失败")

    monkeypatch.setattr(pipeline_engine, "_generate_script", boom)

    async with factory() as s:
        with pytest.raises(RuntimeError) as ei:
            await pipeline_engine.run_stage(s, 1, "script")
    assert "分镜缺失" in str(ei.value), str(ei.value)
    assert called == [], "重试路径不得重新烧 LLM"


# ---------------------------------------------------------------- #
# 3) 反例守护：评分通过时不得误报，且分镜正常落库
# ---------------------------------------------------------------- #

@pytest.mark.asyncio()
async def test_script_proceeds_when_rubric_passed(script_env, monkeypatch):
    factory = script_env
    await _mk_task(factory)

    async def fake_gen(db, task, poem, style="人生感悟", keywords=None):
        task.script = "达标文案正文"
        task.script_score = 9.0
        task.storyboards_json = SB_JSON
        task.storyboard = json.dumps([SHOT], ensure_ascii=False)
        await db.commit()
        return task.script, ScoreResult(score=9.0, feedback="很好", passed=True)

    monkeypatch.setattr(pipeline_engine, "_generate_script", fake_gen)

    async with factory() as s:
        await pipeline_engine.run_stage(s, 1, "script")   # 不应抛错

    async with factory() as s:
        t = await s.get(Task, 1)
        assert t.status != "failed"
        assert t.storyboards_json and json.loads(t.storyboards_json)


# ---------------------------------------------------------------- #
# 4) 幂等：文案 + 分镜齐备 → 跳过，且不调 LLM
# ---------------------------------------------------------------- #

@pytest.mark.asyncio()
async def test_script_skipped_when_both_present(script_env, monkeypatch):
    factory = script_env
    await _mk_task(factory, script="已有文案", storyboards_json=SB_JSON)

    called: list[int] = []

    async def boom(*a, **k):
        called.append(1)
        raise AssertionError("产物齐备时必须跳过")

    monkeypatch.setattr(pipeline_engine, "_generate_script", boom)

    async with factory() as s:
        await pipeline_engine.run_stage(s, 1, "script")   # 不抛
    assert called == []


# ---------------------------------------------------------------- #
# 5) force 重跑：即便分镜齐备也必须重新生成（不得被跳过判据拦住）
# ---------------------------------------------------------------- #

@pytest.mark.asyncio()
async def test_script_force_regenerates_despite_storyboard(script_env, monkeypatch):
    factory = script_env
    await _mk_task(factory, script="旧文案", storyboards_json=SB_JSON)

    called: list[int] = []

    async def fake_gen(db, task, poem, style="人生感悟", keywords=None):
        called.append(1)
        task.script = "强制重生成文案"
        task.storyboards_json = SB_JSON
        await db.commit()
        return task.script, ScoreResult(score=9.0, feedback="ok", passed=True)

    monkeypatch.setattr(pipeline_engine, "_generate_script", fake_gen)

    async with factory() as s:
        await pipeline_engine.run_stage(s, 1, "script", force=True)
    assert called == [1], "force 必须真实重跑"
