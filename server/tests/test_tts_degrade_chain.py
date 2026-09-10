"""CosyVoice→edge-tts 真降级链守护测试（2026-09-10 任务005）。

事故：preset 带参考音频时 engine=auto 强制锁定 cosyvoice，运行库缺失
（No module named 'cosyvoice'）后 _load_cosyvoice 只置标志并打"将降级
edge-tts"日志，但选择逻辑不回头 → raise「CosyVoice2 模型未加载」→
每镜重试 3 次 → 静音兜底 → 无声片。

守护点：
1. preset 强制 cosyvoice 但模型不可用 → 真降级 edge-tts（success=True +
   warning，非 raise），并使用 tts_fallback_voice 音色；
2. 无 preset 且全局参考未配置 → auto 直接选 edge-tts，无降级警告；
3. 模型可用 → 走 cosyvoice 合成（stub），engine=cosyvoice 无警告。
"""
import os
import sys
import asyncio

import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.tts_core as tc
from app.config import settings


@pytest.fixture()
def ref_wav(tmp_path):
    """假参考音频文件（只要求存在，不参与合成）。"""
    p = tmp_path / "ref_test.wav"
    p.write_bytes(b"RIFF0000WAVE")
    return str(p)


def _fake_preset(ref_wav_path):
    return {
        "id": "hongyun", "name": "虹云", "gender": "female",
        "ref_wav": ref_wav_path, "prompt_text": "参考文字",
    }


def test_preset_forced_cosyvoice_unavailable_degrades(monkeypatch, ref_wav_path=None):
    ref_wav_path = ref_wav_path or _ensure_tmp_ref()
    monkeypatch.setattr(tc, "get_preset", lambda pid: _fake_preset(ref_wav_path))
    monkeypatch.setattr(tc, "_load_cosyvoice", lambda: None)

    used_voices = []

    async def fake_edge(text, voice, speed):
        used_voices.append(voice)
        return b"mp3-bytes", 1234

    monkeypatch.setattr(tc, "tts_edge", fake_edge)

    r = asyncio.run(tc.generate_tts(f"测试文本-{uuid.uuid4()}", preset_id="hongyun"))
    assert r["success"] is True, "降级后必须成功而非 raise"
    assert r["engine"] == "edge-tts"
    assert r["warning"] and "降级" in r["warning"]
    assert used_voices == [settings.tts_fallback_voice], "降级必须使用配置的兜底音色"


def _ensure_tmp_ref():
    import tempfile
    fd, p = tempfile.mkstemp(suffix=".wav")
    os.write(fd, b"RIFF0000WAVE")
    os.close(fd)
    return p


def test_auto_without_preset_goes_edge_no_warning(monkeypatch):
    # 全局参考音频未配置（默认 ""）→ auto 直接 edge-tts，无降级警告
    monkeypatch.setattr(tc, "_cosyvoice_reference_ready", lambda: False)

    async def fake_edge(text, voice, speed):
        return b"mp3-bytes", 100

    monkeypatch.setattr(tc, "tts_edge", fake_edge)
    r = asyncio.run(tc.generate_tts(f"测试文本-{uuid.uuid4()}", engine="auto"))
    assert r["success"] is True
    assert r["engine"] == "edge-tts"
    assert not r.get("warning")


def test_cosyvoice_available_uses_cosyvoice(monkeypatch, tmp_path):
    monkeypatch.setattr(tc, "get_preset", lambda pid: _fake_preset(str(tmp_path / "r.wav")))
    (tmp_path / "r.wav").write_bytes(b"RIFF0000WAVE")

    class FakeModel:
        pass

    monkeypatch.setattr(tc, "_load_cosyvoice", lambda: FakeModel())

    def fake_cosy(text, speed, ref_wav_override=None, prompt_text_override=None):
        return b"wav-bytes", 999

    monkeypatch.setattr(tc, "tts_cosyvoice", fake_cosy)

    r = asyncio.run(tc.generate_tts(f"测试文本-{uuid.uuid4()}", preset_id="hongyun"))
    assert r["success"] is True
    assert r["engine"] == "cosyvoice"
    assert not r.get("warning")
    assert r["audio_path"].endswith(".wav"), "cosyvoice 产物必须是 wav 后缀"
