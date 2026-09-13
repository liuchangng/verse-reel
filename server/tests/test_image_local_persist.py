"""2026-09-13 根因修复回归测试：分镜图本地落盘 + 合成阶段本地优先。

覆盖 change-id=image-local-persist 与 composite-local-cache：
- image 阶段把远程图下载到本地并持久化路径（_persist_images_local）
- 合成阶段 _download_storyboard_images 优先用 image_local_paths；
  回退远程时强制落盘为 img_{i}.png（与 _build_segments 命名一致）；
  下载失败记 ERROR 并跳过该张（不再静默丢图导致"所有比例组均未产出成片"）。
"""
import asyncio
import json
import sys
import os
from types import SimpleNamespace
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.pipeline import pipeline_engine


class _FakeResp:
    def __init__(self, content: bytes, ctype: str = "image/png"):
        self.content = content
        self.headers = {"content-type": ctype}
        self.status_code = 200

    def raise_for_status(self):
        return None


CALLS: list = []  # 全局记录 httpx GET 调用，用于断言"未触发下载"


class _FakeAsyncClient:
    """记录 GET 调用；http://fail/ 模拟下载失败。"""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        CALLS.append(url)
        if url.startswith("http://fail/"):
            raise RuntimeError("simulated download error")
        return _FakeResp(b"\x89PNG\r\n\x1a\n" * 20)


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    """隔离 settings：output_dir 指向临时目录，关闭水印避免调 ffmpeg。"""
    s = SimpleNamespace(
        output_dir=str(tmp_path),
        watermark_enabled=False,
        watermark_text="",
        ffmpeg_path="ffmpeg",
        watermark_fontfile="", watermark_colour="white", watermark_alpha=0.3,
        watermark_position="left_bottom", watermark_fontsize_ratio=0.05,
        watermark_margin_ratio=0.03,
    )
    monkeypatch.setattr("app.services.pipeline.settings", s)
    monkeypatch.setattr("app.services.pipeline.httpx.AsyncClient", _FakeAsyncClient)
    return s


def _task(image_urls=None, image_local_paths=None):
    return SimpleNamespace(
        image_urls=json.dumps(image_urls) if image_urls is not None else None,
        image_local_paths=json.dumps(image_local_paths) if image_local_paths is not None else None,
        id=1,
    )


@pytest.mark.asyncio
async def test_local_paths_preferred_no_http(fake_settings, tmp_path):
    """image_local_paths 已存在 → 直接复用，不触发任何 http 下载。"""
    CALLS.clear()
    od = tmp_path / "task_1"
    od.mkdir()
    lp = [str(od / "img_0.png"), str(od / "img_1.png")]
    for p in lp:
        Path(p).write_bytes(b"local-bytes")
    out = await pipeline_engine._download_storyboard_images(_task(image_urls=["http://x/0"], image_local_paths=lp), od)
    assert out == lp
    assert CALLS == []  # 本地优先，未触发任何下载


@pytest.mark.asyncio
async def test_remote_download_forced_png(fake_settings, tmp_path):
    """仅 image_urls（远程）→ 下载并落盘为 img_{i}.png（即使 ctype=jpeg 也强制 png）。"""
    od = tmp_path / "task_1"
    od.mkdir()
    out = await pipeline_engine._download_storyboard_images(_task(image_urls=["http://cdn/0.jpg"]), od)
    assert len(out) == 1
    p = Path(out[0])
    # 命名强制 .png（与 _build_segments 的 img_{i}.png / img_{i}_wm.png 一致）
    assert p.name == "img_0.png"
    assert p.exists() and p.stat().st_size > 0


@pytest.mark.asyncio
async def test_remote_download_failure_skipped_with_error(fake_settings, tmp_path, caplog):
    """远程下载失败 → 显式 ERROR 并跳过该张，返回空列表（不静默）。"""
    import logging
    od = tmp_path / "task_1"
    od.mkdir()
    with caplog.at_level(logging.ERROR):
        out = await pipeline_engine._download_storyboard_images(_task(image_urls=["http://fail/0"]), od)
    assert out == []
    assert any("分镜图下载失败" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_persist_images_local_writes_files(fake_settings, tmp_path):
    """image 阶段落盘：把远程图下载到 data/output/task_{id}/img_{i}.png。"""
    out = await pipeline_engine._persist_images_local(_task(image_urls=["http://cdn/0", "http://cdn/1"]), ["http://cdn/0", "http://cdn/1"])
    assert len(out) == 2
    for p in out:
        assert Path(p).exists() and Path(p).stat().st_size > 0
        assert Path(p).name in ("img_0.png", "img_1.png")


@pytest.mark.asyncio
async def test_persist_best_effort_on_failure(fake_settings, tmp_path):
    """落盘失败不阻断 image 阶段：能下的下，失败的跳过，返回成功的部分。"""
    out = await pipeline_engine._persist_images_local(
        _task(image_urls=["http://cdn/0", "http://fail/1"]),
        ["http://cdn/0", "http://fail/1"],
    )
    assert len(out) == 1
    assert Path(out[0]).name == "img_0.png"
