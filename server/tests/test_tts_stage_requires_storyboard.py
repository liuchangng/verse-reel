"""tts 阶段「无分镜必须 fail-loud」守护测试（2026-09-14 change-id=tts-stage-storyboard-required）。

注意与 ``test_tts_fail_loud.py`` 的区别：那个文件守的是**静音兜底门禁**
（``_tts_loudness_gate``，edge-tts 全失败时拒绝出无声片）；本文件守的是
**tts 阶段缺分镜时不得静默空转成功**，是另一条链路。

事故（实测，新实例 12 分钟内）：
    文案评分未达标 → ``run_stage.script`` 提前 return ⇒ ``storyboards_json`` 不落库
    （``_generate_script`` 只在评分通过时才写）⇒ tts 阶段逐档发现"无分镜"，旧实现
    ``logger.warning + continue`` ⇒ 该 Job 以 **done** 收尾，但 ``task.audio_url``
    始终为空。而 ``queue._task_produced()['tts']`` 以 ``audio_url`` 非空为准 ⇒ 恒判
    "tts 未产出" ⇒ subtitle 的 ``_deps_satisfied`` 永不满足 ⇒ ``queue._heal_prereqs``
    每轮轮询补建一条 tts Job ⇒ **无界自愈死循环**。

    证据：``🩹 自愈入队 ... 补建缺失前置 ['tts']（由 subtitle 触发）`` 358 次；
    ``档位 S 无分镜，跳过该档 TTS`` 456 次；task11 累积 394 条、task16 339 条
    done 的 tts Job（其余 73 个任务各 1 条）；单条 tts Job 空转仅 14ms。

    死循环之所以没被现有防护兜住：``_heal_prereqs`` 只对"前置阶段已 **failed**"
    做级联失败，而空转 Job 是 **done** ⇒ 落进"补建"分支。

修复：某档无分镜时抛 RuntimeError ⇒ Job 落 failed ⇒ 下一次自愈即级联失败、
任务干净落终态，循环终止且原因可见。
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
async def tts_env():
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


# ---------------------------------------------------------------- #
# 1) 全档无分镜 → 必须 fail-loud（旧实现静默 done，正是死循环的源头）
# ---------------------------------------------------------------- #

@pytest.mark.asyncio()
async def test_tts_raises_when_no_storyboard_anywhere(tts_env):
    factory = tts_env
    async with factory() as s:
        # 模拟"文案评分未达标"后的落库状态：有 script 正文，但无 storyboard /
        # storyboards_json（评分未过时 pipeline 不写分镜）
        s.add(Task(id=1, poem_id=1, platform="douyin", script="文案正文「金句」。"))
        await s.commit()

    async with factory() as s:
        with pytest.raises(RuntimeError) as ei:
            await pipeline_engine.run_stage(s, 1, "tts")
    assert "无 storyboard" in str(ei.value), str(ei.value)


# ---------------------------------------------------------------- #
# 2) 部分档位缺分镜 → 同样 fail-loud，且错误里点名缺失档位
# ---------------------------------------------------------------- #

@pytest.mark.asyncio()
async def test_tts_raises_and_names_missing_tier(tts_env):
    factory = tts_env
    async with factory() as s:
        s.add(Task(
            id=1, poem_id=1, platform="douyin", script="文案正文。",
            storyboards_json=json.dumps({
                "S": {"script": "快档", "storyboard": [SHOT]},
                "L": {"script": "深档", "storyboard": []},   # ← 缺失
            }, ensure_ascii=False),
        ))
        await s.commit()

    async with factory() as s:
        with pytest.raises(RuntimeError) as ei:
            await pipeline_engine.run_stage(s, 1, "tts")
    assert "档位 L" in str(ei.value), str(ei.value)


# ---------------------------------------------------------------- #
# 3) 反例守护：全档分镜齐备时不得误报（防止"修过头"）
# ---------------------------------------------------------------- #

@pytest.mark.asyncio()
async def test_tts_proceeds_when_all_tiers_have_storyboard(tts_env, monkeypatch):
    factory = tts_env
    async with factory() as s:
        s.add(Task(
            id=1, poem_id=1, platform="douyin", script="文案正文。",
            storyboards_json=json.dumps({
                "S": {"script": "快档", "storyboard": [SHOT]},
                "L": {"script": "深档", "storyboard": [SHOT]},
            }, ensure_ascii=False),
        ))
        await s.commit()

    calls: list[str] = []

    async def fake_tts(task, storyboard, db=None, force=False, tier=""):
        calls.append(tier)
        return {"success": True, "segments": []}

    monkeypatch.setattr(pipeline_engine, "_generate_tts_segments", fake_tts)

    async with factory() as s:
        await pipeline_engine.run_stage(s, 1, "tts")   # 不应抛错
    # 两个档位都被执行（主档 S 的 label=""，非主档 L 的 label="L"）
    assert sorted(calls) == ["", "L"], calls
