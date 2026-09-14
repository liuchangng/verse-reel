"""subtitle 阶段必须依赖 image（修「分镜图未落盘就被认领」竞态）。

背景（2026-09-14，change-id=subtitle-image-prereq）
--------------------------------------------------
``STAGE_PREREQS["subtitle"]`` 原为 ``{"video", "tts"}``。而 ``enable_agnes_video=False``
（``app/config.py`` 默认）时 video 阶段被 ``_stage_enabled`` 过滤掉 ⇒ subtitle 的
有效前置只剩 ``{tts}`` ⇒ **image 不在前置链上**。

但 subtitle 阶段本体就是「分镜图 + 旁白 → 合成成片」（``_render_platform_outputs``），
必须等 image 产物落盘。于是它与 image 并发竞态：分镜图尚未落盘就被认领，报

    逐镜合成无可用的(图,音频)对: 分镜图本地可用 0/7，音频本地可用 7/7

3 次重试用尽 → 永久失败。实测 75 条课标任务中 task3/4/5/6/7 接连中招
（task1 仅因 image 恰好先跑完而侥幸成功）。

本模块锁死「subtitle 必须等 image 产物落盘 **且** image Job 收工」。
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.services.queue import (
    STAGE_PREREQS,
    _stage_enabled,
    _task_produced,
    queue_service,
)


class _Res:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    """只满足 ``_deps_satisfied`` 的两个调用：session.get / session.execute。"""

    def __init__(self, task, active_stages=()):
        self._task = task
        self._active = list(active_stages)

    async def get(self, model, pk):
        return self._task

    async def execute(self, stmt):
        return _Res(self._active)


def _task(**kw):
    base = dict(
        id=1,
        script="正文文案",
        character_ref="https://x/ref.png",
        image_urls=None,
        image_local_paths=None,
        audio_url="https://x/a.mp3",
        video_url=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _deps(task, stage="subtitle", active_stages=()):
    job = SimpleNamespace(stage=stage, task_id=task.id)
    return asyncio.run(queue_service._deps_satisfied(_FakeSession(task, active_stages), job))


def test_subtitle_declares_image_prereq():
    """静态契约：subtitle 必须把 image 列为前置（video 禁用时的唯一兜底）。"""
    assert "image" in STAGE_PREREQS["subtitle"]


def test_image_survives_video_disabled_filtering():
    """video 禁用时，subtitle 的有效前置仍必须含 image（旧缺陷即此处丢失）。"""
    if _stage_enabled("video"):
        pytest.skip("video 阶段启用时该场景不成立（image 仍应保留）")
    eff = {p for p in STAGE_PREREQS["subtitle"] if _stage_enabled(p)}
    assert "image" in eff, "video 禁用时 subtitle 仍必须依赖 image"
    assert "tts" in eff


def test_subtitle_blocked_until_image_produced():
    """分镜图完全未生成 ⇒ subtitle 依赖不满足（不得认领）。"""
    assert _task_produced(_task())["image"] is False
    assert _deps(_task()) is False


def test_subtitle_blocked_until_local_images_landed():
    """仅有 CDN URL、本地未落盘 ⇒ 仍不放行（合成阶段取的是本地图）。"""
    t = _task(image_urls='["https://x/0.png"]', image_local_paths=None)
    assert _task_produced(t)["image"] is False
    assert _deps(t) is False


def test_subtitle_blocked_while_image_job_inflight():
    """产物已就绪但 image Job 仍在途 ⇒ 不放行（在途阻断）。"""
    t = _task(image_urls='["https://x/0.png"]', image_local_paths='["p0.png"]')
    assert _task_produced(t)["image"] is True
    assert _deps(t, active_stages=["image"]) is False


def test_subtitle_allowed_when_image_and_tts_ready():
    """image 与 tts 均就绪且无在途 ⇒ 放行。"""
    t = _task(image_urls='["https://x/0.png"]', image_local_paths='["p0.png"]')
    assert _deps(t) is True
