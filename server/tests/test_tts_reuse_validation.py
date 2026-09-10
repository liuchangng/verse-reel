"""TTS 分段复用校验守护测试（2026-09-09 事故）。

事故：TTS 3 连失败时 anullsrc 静音兜底文件（narration_N.mp3，-91dB）因
"文件存在"被复用逻辑无限采纳，22:05 字幕合成直接复用 20:29 的静音分段，
成片全程无声（final.mp4 仅剩 -37dB 的 BGM）。

修复语义：复用校验 = 条数一致 + 逐条文本一致 + 文件存在 + 非静音/无
fallback 标记；任一不满足则重新生成。
"""
import json
import subprocess
from pathlib import Path

import pytest

from app.config import settings
from app.services import pipeline as pmod
from app.services.pipeline import PipelineEngine
from app.models.task import Task


def _make_audio(path: Path, kind: str):
    """kind: 'silent' → anullsrc 数字静音；'sound' → 440Hz 正弦波。"""
    src = (
        "anullsrc=r=24000:cl=mono:d=0.5" if kind == "silent"
        else "sine=frequency=440:duration=0.5"
    )
    subprocess.run(
        [settings.ffmpeg_path, "-y", "-f", "lavfi", "-i", src,
         "-c:a", "libmp3lame", str(path)],
        capture_output=True, text=True, timeout=30, check=True,
    )


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(pmod.settings, "output_dir", str(tmp_path))
    task = Task(id=99, poem_id=1, platform="douyin", script="x", status="processing")
    storyboard = [{"narration": "第一镜旁白"}, {"narration": "第二镜旁白"}]

    calls = {"generate": 0}

    sound_file = tmp_path / "_sine_fixture.mp3"
    _make_audio(sound_file, "sound")

    async def fake_health():
        return True

    async def fake_generate(**kwargs):
        calls["generate"] += 1
        return {"success": True, "engine": "edge-tts",
                "audio_path": str(sound_file), "audio_url": None,
                "duration_ms": 500, "error": None}

    monkeypatch.setattr(pmod.tts_client, "health_check", fake_health)
    monkeypatch.setattr(pmod.tts_client, "generate", fake_generate)
    return task, storyboard, tmp_path, calls


@pytest.mark.asyncio()
async def test_is_silent_audio_detects_anullsrc_and_sine(tmp_path):
    """静音检测：anullsrc 产物判静音；正弦波/不存在文件不误判。"""
    silent = tmp_path / "s.mp3"
    sound = tmp_path / "v.mp3"
    _make_audio(silent, "silent")
    _make_audio(sound, "sound")
    assert PipelineEngine._is_silent_audio(silent) is True
    assert PipelineEngine._is_silent_audio(sound) is False
    assert PipelineEngine._is_silent_audio(tmp_path / "nope.mp3") is False


@pytest.mark.asyncio()
async def test_silent_fallback_segments_are_regenerated(env):
    """历史静音分段（无标记的 anullsrc 文件）必须被拒绝复用并重新生成。"""
    task, storyboard, out_dir, calls = env
    tdir = out_dir / "task_99"
    tdir.mkdir()
    seg_json = tdir / "tts_segments.json"
    entries = []
    for i in range(2):
        f = tdir / f"narration_{i}.mp3"
        _make_audio(f, "silent")  # 20:29 事故同款静音兜底文件
        entries.append({"index": i, "text": f"第{'一二'[i]}镜旁白",
                        "duration": 0.5, "path": str(f)})
    seg_json.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    engine = PipelineEngine()
    r = await engine._generate_tts_segments(task, storyboard, None)

    assert calls["generate"] == 2, "静音分段必须触发重新生成而非复用"
    assert r["success"] and len(r["segments"]) == 2
    assert all(not s.get("fallback") for s in r["segments"]), "重生成后不得残留 fallback 标记"
    saved = json.loads(seg_json.read_text(encoding="utf-8"))
    assert all(not s.get("fallback") for s in saved)


@pytest.mark.asyncio()
async def test_fallback_flagged_segments_are_regenerated(env):
    """即使文件有声，带 fallback 标记的分段也不得复用。"""
    task, storyboard, out_dir, calls = env
    tdir = out_dir / "task_99"
    tdir.mkdir()
    entries = []
    for i in range(2):
        f = tdir / f"narration_{i}.mp3"
        _make_audio(f, "sound")
        entries.append({"index": i, "text": f"第{'一二'[i]}镜旁白",
                        "fallback": True, "duration": 0.5, "path": str(f)})
    (tdir / "tts_segments.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    engine = PipelineEngine()
    r = await engine._generate_tts_segments(task, storyboard, None)
    assert calls["generate"] == 2, "fallback 标记分段必须重新生成"


@pytest.mark.asyncio()
async def test_healthy_segments_still_reused(env):
    """健康分段（有声 + 文本一致 + 无标记 + 音色指纹匹配）仍走复用，不浪费 TTS 配额。"""
    task, storyboard, out_dir, calls = env
    tdir = out_dir / "task_99"
    tdir.mkdir()
    # 由当前代码计算音色指纹，模拟"同一音色身份下本代码刚写出的分段"
    fp = PipelineEngine()._tts_voice_fingerprint(task)
    entries = []
    for i in range(2):
        f = tdir / f"narration_{i}.mp3"
        _make_audio(f, "sound")
        entries.append({"index": i, "text": f"第{'一二'[i]}镜旁白",
                        "duration": 0.5, "path": str(f), "voice_fp": fp})
    (tdir / "tts_segments.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    engine = PipelineEngine()
    r = await engine._generate_tts_segments(task, storyboard, None)
    assert calls["generate"] == 0, "音色身份未变的健康分段不得触发 TTS 调用"
    assert r["success"] and len(r["segments"]) == 2


@pytest.mark.asyncio()
async def test_voice_fingerprint_change_forces_regen(env):
    """音色身份变化（如 preset ref_wav 由带 BGM 的 ref_hongyun.wav 换成
    人声分离版 ref_hongyun_clean.wav）必须强制重合成，不复用旧杂音旁白。
    （2026-09-10 任务005：clean ref 已生成但成片仍用旧 BGM-ref 旁白）"""
    task, storyboard, out_dir, calls = env
    tdir = out_dir / "task_99"
    tdir.mkdir()
    cur_fp = PipelineEngine()._tts_voice_fingerprint(task)
    # 旧分段带的是"另一个音色身份"的指纹（模拟 ref 换掉前合成）
    stale_fp = cur_fp + "\x1f" + "ref_hongyun.wav"  # 必然 != cur_fp
    entries = []
    for i in range(2):
        f = tdir / f"narration_{i}.mp3"
        _make_audio(f, "sound")
        entries.append({"index": i, "text": f"第{'一二'[i]}镜旁白",
                        "duration": 0.5, "path": str(f), "voice_fp": stale_fp})
    (tdir / "tts_segments.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    engine = PipelineEngine()
    r = await engine._generate_tts_segments(task, storyboard, None)
    assert calls["generate"] == 2, "音色身份变化必须重新生成旁白"
    # 重合成后写回的分段指纹 = 当前指纹
    saved = json.loads((tdir / "tts_segments.json").read_text(encoding="utf-8"))
    assert all(s.get("voice_fp") == cur_fp for s in saved), "新分段应带当前音色指纹"


@pytest.mark.asyncio()
async def test_missing_voice_fp_legacy_forces_regen_once(env):
    """旧代码写出的分段（无 voice_fp 键）判为过期 → 一次性迁移重合成。
    这是任务005 的真实场景：clean ref 落地前合成、无指纹的旧 BGM-ref 旁白。"""
    task, storyboard, out_dir, calls = env
    tdir = out_dir / "task_99"
    tdir.mkdir()
    entries = []
    for i in range(2):
        f = tdir / f"narration_{i}.mp3"
        _make_audio(f, "sound")
        # 故意不写 voice_fp 键，模拟旧代码产物
        entries.append({"index": i, "text": f"第{'一二'[i]}镜旁白",
                        "duration": 0.5, "path": str(f)})
    (tdir / "tts_segments.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    engine = PipelineEngine()
    r = await engine._generate_tts_segments(task, storyboard, None)
    assert calls["generate"] == 2, "缺音色指纹的旧分段必须重新生成（迁移）"


@pytest.mark.asyncio()
async def test_text_mismatch_rejects_reuse(env):
    """文案已改（分段数相同但旁白文本不同）不得复用旧旁白。"""
    task, storyboard, out_dir, calls = env
    tdir = out_dir / "task_99"
    tdir.mkdir()
    entries = []
    for i in range(2):
        f = tdir / f"narration_{i}.mp3"
        _make_audio(f, "sound")
        entries.append({"index": i, "text": "旧文案旁白内容",
                        "duration": 0.5, "path": str(f)})
    (tdir / "tts_segments.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    engine = PipelineEngine()
    r = await engine._generate_tts_segments(task, storyboard, None)
    assert calls["generate"] == 2, "文本不一致必须重新生成"
