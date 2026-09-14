"""``_task_produced()['script']`` 判据必须「文案 + 分镜」双条件 守护测试。

change-id: script-produce-needs-storyboard（2026-09-14）

事故：旧判据只看 ``task.script`` 非空，而 ``_generate_script`` 恒落库文案、
仅在主档评分通过时才写分镜 ⇒「文案评分未达标」的任务被误判为 script 已产出：
- 入队 ``_expand_prereqs`` 不再补建 script 前置；
- 消费侧 ``_deps_satisfied`` 放行 character/image/tts/subtitle，
  白跑若干阶段（无效生图 / 无效 TTS）直到缺分镜才兜底失败。

下游阶段真正需要的前置是「文案 + 分镜」（image/tts 都以分镜为输入），
故判据必须与之一致。修正后这类任务会在最短路径上由「script 前置已 failed」
触发 ``_cascade_fail_pending``，任务干净落终态、不再无谓烧资源。
"""
from app.models.task import Task
from app.services.queue import STAGE_ORDER, _expand_prereqs, _task_produced


# ---------------------------------------------------------------- #
# 1) 有文案、无分镜 → 必须判为未产出（旧实现误判 True）
# ---------------------------------------------------------------- #

def test_script_without_storyboard_not_produced():
    task = Task(id=1, poem_id=1, platform="douyin", script="文案正文")
    produced = _task_produced(task)
    assert produced["script"] is False, "缺分镜时 script 不得视为已产出"


# ---------------------------------------------------------------- #
# 2) 文案 + task.storyboard → 已产出
# ---------------------------------------------------------------- #

def test_script_with_task_storyboard_produced():
    task = Task(id=1, poem_id=1, platform="douyin",
                script="文案正文", storyboard='[{"time": "0-3s"}]')
    assert _task_produced(task)["script"] is True


# ---------------------------------------------------------------- #
# 3) 文案 + storyboards_json（多档口径）→ 已产出
# ---------------------------------------------------------------- #

def test_script_with_storyboards_json_produced():
    task = Task(id=1, poem_id=1, platform="douyin",
                script="文案正文",
                storyboards_json='{"S": {"storyboard": [{"time": "0-3s"}]}}')
    assert _task_produced(task)["script"] is True


# ---------------------------------------------------------------- #
# 4) 无文案 → 未产出（原判据行为不得回归）
# ---------------------------------------------------------------- #

def test_script_absent_not_produced():
    task = Task(id=1, poem_id=1, platform="douyin")
    assert _task_produced(task)["script"] is False


# ---------------------------------------------------------------- #
# 5) 集成：缺分镜时入队必须补建 script 前置（否则下游白跑）
# ---------------------------------------------------------------- #

def test_expand_prereqs_backfills_script_when_storyboard_missing():
    """重跑 image 而 script 只有文案没有分镜 → 必须把 script 一并补建。"""
    task = Task(id=1, poem_id=1, platform="douyin",
                script="文案正文",                      # 有文案
                character_ref="http://img/char.png",   # 定妆照已在
                image_urls='["http://cdn/0.png"]',
                image_local_paths='["task_1/img_0.png"]')
    expanded = _expand_prereqs(["image"], _task_produced(task))
    assert "script" in expanded, "分镜缺失时必须补建 script 前置"
    assert expanded == [s for s in STAGE_ORDER if s in set(expanded)]
