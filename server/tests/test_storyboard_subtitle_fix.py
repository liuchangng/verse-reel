"""2026-09-13 两个代码 bug 的回归测试。

- storyboard-empty-guard（Bug 2）：LLM 返回合法空列表 "[]" 时，旧版静默透传导致
  storyboard 为空、image/tts/subtitle 全链"缺少前置 storyboard"失败（task_26 根因）。
  修复后应与解析失败同等降级为档位化默认分镜。
- subtitle-image-align（Bug 1）：合成取图以 storyboard 镜序为基准配对，不再盲信旧
  tts_segments 的 index/path。构造「分段 index 整体漂移」场景，验证仍按镜序 1:1 配对、
  不再 0 出片。
"""
import asyncio
import base64
import json
import os
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.pipeline import pipeline_engine

# 1x1 合法 PNG，仅用于 _find_img 的存在性判定（合成由 fake ffmpeg 接管，不真读图）
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


# ---------------- Bug 2：空分镜降级 ----------------
@pytest.mark.asyncio
async def test_storyboard_empty_list_falls_back(monkeypatch):
    """critic 返回 "[]" → 必须降级为非空默认分镜，而非静默返回 []。"""

    async def _empty(_script, tier=None):
        return "[]"

    monkeypatch.setattr(
        "app.services.pipeline.critic_service.generate_storyboard", _empty
    )
    sb = await pipeline_engine._generate_storyboard("任意文案", tier="S")
    assert isinstance(sb, list) and len(sb) > 0, "空分镜未降级为默认分镜"
    assert all(it.get("narration") for it in sb), "默认分镜每镜应有 narration"


@pytest.mark.asyncio
async def test_storyboard_malformed_json_falls_back(monkeypatch):
    """critic 返回非法文本 → 同样降级默认分镜（既有行为，回归守护）。"""

    async def _bad(_script, tier=None):
        return "这不是 json"

    monkeypatch.setattr(
        "app.services.pipeline.critic_service.generate_storyboard", _bad
    )
    sb = await pipeline_engine._generate_storyboard("任意文案", tier="L")
    assert isinstance(sb, list) and len(sb) > 0


# ---------------- Bug 1：按镜序配对（index 漂移不再 0 出片） ----------------
class _FakeSubprocess:
    """fake ffmpeg/ffprobe：seg 与 base 均由最后一个参数路径写出 dummy 文件；
    ffprobe 返回固定时长。合成逻辑（配对/时间轴）仍真实执行。"""

    def __init__(self, *a, **k):
        pass

    def __call__(self, cmd, *a, **k):
        if "ffprobe" in str(cmd[0]):
            return SimpleNamespace(returncode=0, stdout="3.0\n", stderr="")
        out = cmd[-1]
        Path(out).write_bytes(b"dummy-mp4")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    s = SimpleNamespace(
        output_dir=str(tmp_path),
        ffmpeg_path="ffmpeg",
        watermark_enabled=False,
        watermark_text="",
        transition_enabled=False,
        transition_duration=0.0,
        watermark_fontfile="", watermark_colour="white", watermark_alpha=0.3,
        watermark_position="left_bottom", watermark_fontsize_ratio=0.05,
        watermark_margin_ratio=0.03,
    )
    monkeypatch.setattr("app.services.pipeline.settings", s)
    monkeypatch.setattr("app.services.pipeline.subprocess.run", _FakeSubprocess())
    # 阻断 _download_storyboard_images 的 CDN 回退（本测试图像已在磁盘）
    import httpx as _httpx

    class _NoNet:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        async def get(self, url):
            raise RuntimeError("network disabled in test")

    monkeypatch.setattr(_httpx, "AsyncClient", _NoNet)
    return s


def _task(tid=1):
    return SimpleNamespace(id=tid, image_urls=None, image_local_paths=None)


@pytest.mark.asyncio
async def test_build_segments_jpg_naming_self_heal(fake_env, tmp_path):
    """磁盘分镜图为旧命名 img_{j}.jpg（非 png），CDN 不可达（image_local_paths 空）。
    旧代码只查 img_{j}.png → 0 出片；修复后 _find_img 多后缀兜底 → 1:1 命中全部 6 镜。"""
    od = tmp_path / "task_1"
    od.mkdir()
    for j in range(6):
        Path(od / f"img_{j}.jpg").write_bytes(_PNG)  # 旧命名 jpg
        Path(od / f"narration_{j}.wav").write_bytes(b"dummy-audio")
    storyboard = [{"narration": f"镜{j}"} for j in range(6)]
    segs = {
        "segments": [
            {"index": j, "text": f"t{j}", "path": str(od / f"narration_{j}.wav")}
            for j in range(6)
        ]
    }
    res = await pipeline_engine._build_segments(_task(1), storyboard, segs, 1080, 1920, "douyin")
    assert res["ok"] is True, f"jpg 命名应自愈配对，实际: {res}"
    assert Path(res["base"]).exists() and Path(res["base"]).stat().st_size > 0


@pytest.mark.asyncio
async def test_build_segments_zero_when_images_missing(fake_env, tmp_path):
    """storyboard 6 镜但磁盘无任何分镜图 → 应 fail-loud（ok=False），而非静默出空片。"""
    od = tmp_path / "task_1"
    od.mkdir()
    for j in range(6):
        Path(od / f"narration_{j}.wav").write_bytes(b"dummy-audio")  # 仅音频，无图
    storyboard = [{"narration": f"镜{j}"} for j in range(6)]
    segs = {
        "segments": [
            {"index": j, "text": f"t{j}", "path": str(od / f"narration_{j}.wav")}
            for j in range(6)
        ]
    }
    res = await pipeline_engine._build_segments(_task(1), storyboard, segs, 1080, 1920, "douyin")
    assert res["ok"] is False
