"""TTS 核心（进程内，合并自独立 tts-server）。

- edge-tts：默认引擎，无需 torch，联网即用。
- CosyVoice2（阿里通义开源）：高质量中文 TTS，懒加载，需先下载权重
  （server/pretrained_models/CosyVoice2-0.5B）并安装 cosyvoice 包。
  torch / torchaudio 复用 Audio2Sheet 的 3.12 venv（cp312 ABI 一致）。

对外契约与原 tts-server 保持一致：generate 返回
{success, engine, audio_url, duration_ms, error}，并额外返回
audio_path（本地产物绝对路径）供 pipeline 直接落盘，省去一次 HTTP 回环。
"""
import os
import sys
import json
import io
import hashlib
import asyncio
import logging
import threading
from pathlib import Path
from typing import Optional

from app.config import settings, DATA_DIR

logger = logging.getLogger(__name__)

# 音频产物目录（与 server/data 统一）：server/data/tts_audio
OUTPUT_DIR = DATA_DIR / "tts_audio"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# CosyVoice2 模型目录：默认 server/pretrained_models/CosyVoice2-0.5B，
# 支持环境变量 COSYVOICE_MODEL_DIR 覆盖。
def _default_model_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "pretrained_models" / "CosyVoice2-0.5B"

MODEL_DIR = Path(os.environ.get("COSYVOICE_MODEL_DIR") or str(_default_model_dir()))


# CosyVoice2 模型（延迟加载，避免启动即 import 重 torch）
_cosyvoice_model = None
_cosyvoice_available = False
# 懒加载线程安全锁：并发请求首次触发加载时，避免重复实例化 4.6GB 模型。
_cosyvoice_load_lock = threading.Lock()


def _ensure_cosyvoice_on_path() -> None:
    """把 third_party/CosyVoice 及其 Matcha-TTS 子模块注入 sys.path。

    背景（2026-09-10）：cosyvoice 不在 PyPI，pyproject 的 [cosyvoice] extra
    只覆盖其第三方依赖，本体必须来自 Git 仓库。采用「clone 到
    server/third_party/CosyVoice（--recursive 含 Matcha-TTS 子模块）+ 运行时
    sys.path 注入」而非 uv git 源安装——uv 不拉子模块，装上也 import 不了；
    且纯 sys.path 注入不进 uv.lock，不会被 uv sync 剪掉。
    """
    repo = Path(__file__).resolve().parents[2] / "third_party" / "CosyVoice"
    candidates = [repo, repo / "third_party" / "Matcha-TTS"]
    for p in candidates:
        sp = str(p)
        if p.exists() and sp not in sys.path:
            sys.path.insert(0, sp)


def _load_cosyvoice():
    """延迟加载 CosyVoice2 模型，返回模型或 None（失败降级）。"""
    global _cosyvoice_model, _cosyvoice_available

    if _cosyvoice_model is not None:
        return _cosyvoice_model

    # 双重检查 + 锁：首个并发请求到达时才真正加载，后续直接复用。
    with _cosyvoice_load_lock:
        if _cosyvoice_model is not None:
            return _cosyvoice_model
        try:
            import torch  # 复用 Audio2Sheet venv 的 torch（cp312/cu128）

            _ensure_cosyvoice_on_path()
            from cosyvoice.cli.cosyvoice import CosyVoice2

            if not MODEL_DIR.exists():
                logger.warning(f"CosyVoice2 模型目录不存在: {MODEL_DIR}（请下载 FunAudioLLM/CosyVoice2-0.5B）")
                _cosyvoice_available = False
                return None

            # torchaudio 2.11 忽略 backend='soundfile' 参数，强行走 torchcodec
            # （需 FFmpeg / CUDA DLL，本机缺 → 加载参考音频即崩）。
            # 直接拦截 torchaudio.load：对常见音频容器（wav/mp3/flac/ogg）改走
            # soundfile(libsndfile，已随 venv 安装)，免 FFmpeg。该拦截在 cosyvoice
            # 加载时一次性生效，覆盖 frontend 等所有 import 绑定点。
            try:
                import soundfile as _sf
                import torchaudio as _ta

                _orig_ta_load = getattr(_ta, "load", None)
                if _orig_ta_load is not None and not getattr(_orig_ta_load, "_cosy_patched", False):

                    def _patched_ta_load(uri, *args, **kwargs):
                        if (kwargs.get("backend") == "soundfile"
                                or str(uri).lower().endswith((".wav", ".mp3", ".flac", ".ogg"))):
                            speech, sample_rate = _sf.read(uri, dtype="float32", always_2d=True)
                            # 对齐 torchaudio.load 默认 channels_first 返回 (C, N)
                            speech = torch.from_numpy(speech).transpose(0, 1)
                            return speech, int(sample_rate)
                        return _orig_ta_load(uri, *args, **kwargs)

                    _patched_ta_load._cosy_patched = True
                    _ta.load = _patched_ta_load
                    logger.info("已拦截 torchaudio.load 走 soundfile 后端（免 FFmpeg）")
            except Exception as _patch_err:
                logger.warning("torchaudio.load 拦截失败，回退默认后端: %s", _patch_err)

            logger.info("加载 CosyVoice2 模型: %s", MODEL_DIR)
            _cosyvoice_model = CosyVoice2(str(MODEL_DIR))
            _cosyvoice_available = True
            logger.info("CosyVoice2 模型加载完成")
            return _cosyvoice_model

        except Exception as e:
            logger.error(f"CosyVoice2 加载失败（将降级 edge-tts）: {e}")
            _cosyvoice_available = False
            return None


def _cosyvoice_reference_ready() -> bool:
    """CosyVoice2 是否可真正合成：已配置参考音频 且 模型成功加载。

    - 若未配置参考音频（wav 不存在），直接返回 False 且不加载重模型，
      让 auto 模式优雅降级 edge-tts。
    - 若已配置参考音频，则尝试加载模型，仅当两者皆满足才返回 True。
    """
    wav = getattr(settings, "cosyvoice_prompt_wav", "") or ""
    if not wav or not Path(wav).exists():
        return False
    # 参考音频已就绪，尝试加载模型（懒加载，失败则降级）。
    if _cosyvoice_model is None:
        _load_cosyvoice()
    return _cosyvoice_available and _cosyvoice_model is not None


def prewarm_cosyvoice():
    """启动预热：若已配置参考音频，在后台线程加载 CosyVoice2 模型，
    使首个 TTS 请求免去 ~14s 模型加载。失败不影响启动（auto 会降级 edge-tts）。

    注意：仅做「参考音频是否存在」的廉价检查，不在此同步加载模型；
    真正加载放到 daemon 线程，避免阻塞 lifespan 启动。
    """
    wav = getattr(settings, "cosyvoice_prompt_wav", "") or ""
    if not wav or not Path(wav).exists():
        logger.info("CosyVoice2 未配置参考音频，跳过预热（auto 将走 edge-tts）")
        return

    def _worker():
        try:
            logger.info("预热加载 CosyVoice2 模型...")
            _load_cosyvoice()
            logger.info("CosyVoice2 预热完成")
        except Exception as e:
            logger.warning("CosyVoice2 预热失败（不影响启动）: %s", e)

    threading.Thread(target=_worker, name="cosyvoice-prewarm", daemon=True).start()


# ====== Voice Preset 系统 ======
# 将调研锁定的朗读音色（方明/濮存昕/丁建华等）固化为可切换配置。
# 每个 preset 含 id/name/gender/style/ref_wav/prompt_text；
# 调用 generate_tts(preset_id=...) 时自动解析 ref_wav+prompt_text 覆盖全局设置。
_presets_cache: Optional[list[dict]] = None
_presets_cache_mtime: float = 0.0
_presets_lock = threading.Lock()


def _get_preset_file() -> Path:
    """预设文件路径（config voice_preset_file，默认 DATA_DIR/voice_presets.json）。"""
    p = getattr(settings, "voice_preset_file", "") or ""
    if p:
        fp = Path(p)
        if fp.is_absolute():
            return fp
        # 相对路径：若含目录组件（如 "presets/voice.json"），相对于项目根；
        # 若纯文件名（如 "voice_presets.json"），相对于 DATA_DIR。
        if fp.name != p:  # 含目录组件
            candidate = Path(__file__).resolve().parents[2] / p
            if candidate.exists():
                return candidate
        return DATA_DIR / fp.name
    return DATA_DIR / "voice_presets.json"


def _load_presets(force: bool = False) -> list[dict]:
    """加载 voice_presets.json，带文件修改时间缓存（避免每次调用都读盘）。"""
    global _presets_cache, _presets_cache_mtime
    pfile = _get_preset_file()
    try:
        mtime = pfile.stat().st_mtime if pfile.exists() else 0
    except OSError:
        mtime = 0
    if not force and _presets_cache is not None and mtime <= _presets_cache_mtime:
        return _presets_cache
    with _presets_lock:
        # double-check after acquiring lock
        try:
            mtime2 = pfile.stat().st_mtime if pfile.exists() else 0
        except OSError:
            mtime2 = 0
        if not force and _presets_cache is not None and mtime2 <= _presets_cache_mtime:
            return _presets_cache
        if pfile.exists():
            try:
                data = json.loads(pfile.read_text(encoding="utf-8"))
                presets = data.get("presets", []) if isinstance(data, dict) else []
                _presets_cache = presets
                _presets_cache_mtime = mtime2
                logger.info("已加载 %d 个 voice preset（%s）", len(presets), pfile)
                return presets
            except Exception as e:
                logger.warning("加载 voice_presets.json 失败: %s", e)
                return _presets_cache or []
        _presets_cache = []
        _presets_cache_mtime = mtime2
        return []


def get_preset(preset_id: str) -> Optional[dict]:
    """按 id 查找 preset，返回 dict 或 None。"""
    for p in _load_presets():
        if p.get("id") == preset_id:
            return p
    return None


def list_presets() -> list[dict]:
    """列出所有可用 preset（含 ref_wav 是否就绪的状态标记）。"""
    result = []
    for p in _load_presets():
        entry = {
            "id": p.get("id"),
            "name": p.get("name"),
            "gender": p.get("gender"),
            "style": p.get("style"),
            "description": p.get("description", ""),
            "recommended_for": p.get("recommended_for", []),
            "source_note": p.get("source_note", ""),
            "has_ref_audio": bool(p.get("ref_wav") and Path(p["ref_wav"]).exists()),
        }
        result.append(entry)
    return result


async def tts_edge(text: str, voice: str, speed: float) -> tuple[bytes, int]:
    """使用 edge-tts 生成语音（懒导入 edge_tts，保证缺包时服务仍可启动）。"""
    import edge_tts

    communicate = edge_tts.Communicate(text, voice, rate=f"+{int((speed - 1) * 100)}%")
    audio_data = b""
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_data += chunk["data"]
    return audio_data, len(audio_data)


def tts_cosyvoice(text: str, speed: float = 1.0,
                  ref_wav_override: Optional[str] = None,
                  prompt_text_override: Optional[str] = None) -> tuple[bytes, int]:
    """使用 CosyVoice2 生成语音（zero-shot 克隆参考音色）。

    CosyVoice2-0.5B 是基础模型，无预设说话人（'中文女' 等 SFT 说话人属于
    CosyVoice 1），必须提供一段参考音频 + 其文字走 inference_zero_shot（或
    inference_instruct2）。参考音频由 settings.cosyvoice_prompt_wav / _prompt_text 配置，
    或由调用方通过 ref_wav_override / prompt_text_override 覆盖（voice preset 场景）。
    """
    import numpy as np
    import soundfile as sf

    # 参考音频来源优先级：preset 覆盖 > 全局 settings
    prompt_wav = ref_wav_override or getattr(settings, "cosyvoice_prompt_wav", "") or ""
    prompt_text = prompt_text_override or getattr(settings, "cosyvoice_prompt_text", "") or ""
    instruct_text = getattr(settings, "cosyvoice_instruct_text", "") or ""
    if not prompt_wav or not Path(prompt_wav).exists():
        raise RuntimeError(
            "CosyVoice2 未配置参考音频（settings.cosyvoice_prompt_wav 不存在），"
            "基础模型无法合成；请放置一段参考音频并配置 cosyvoice_prompt_wav / cosyvoice_prompt_text"
        )

    model = _load_cosyvoice()
    if model is None:
        raise RuntimeError("CosyVoice2 模型未加载")

    if instruct_text:
        output = model.inference_instruct2(
            text, instruct_text, prompt_wav, stream=False, speed=speed
        )
    else:
        if not prompt_text:
            raise RuntimeError("CosyVoice2 zero-shot 需要 cosyvoice_prompt_text（参考音频文字）")
        output = model.inference_zero_shot(
            text, prompt_text, prompt_wav, stream=False, speed=speed
        )

    audio_chunks = [chunk["tts_speech"] for chunk in output]
    audio = np.concatenate(audio_chunks, axis=0)

    buf = io.BytesIO()
    sf.write(buf, audio.squeeze(), 24000, format="WAV")
    buf.seek(0)
    return buf.read(), int(len(audio.squeeze()) * 1000 / 24000)


async def generate_tts(
    text: str,
    voice: str = "zh-CN-YunxiNeural",
    speed: float = 1.0,
    engine: str = "auto",
    preset_id: Optional[str] = None,
) -> dict:
    """生成语音，返回 {success, engine, audio_url, audio_path, duration_ms, error}。

    engine: auto | cosyvoice | edge-tts
    preset_id: 可选的 voice preset id（从 voice_presets.json 解析 ref_wav+prompt_text）。
               传入时覆盖全局 cosyvoice_prompt_wav / _prompt_text；未配置 ref_wav 的 preset
               会像无参考音频一样报错（CosyVoice2 基础模型必须要有参考音频）。
    """
    try:
        chosen = engine

        # 解析 voice preset（若有）：提取 ref_wav + prompt_text 覆盖全局设置
        _ref_wav_ov = _prompt_text_ov = None
        if preset_id and chosen in ("cosyvoice", "auto"):
            preset = get_preset(preset_id)
            if preset:
                _ref_wav_ov = preset.get("ref_wav") or None
                _prompt_text_ov = preset.get("prompt_text") or None
                logger.info("使用 voice preset '%s' (%s)", preset_id, preset.get("name", ""))
            else:
                logger.warning("voice preset '%s' 不存在，忽略", preset_id)

        if chosen == "auto":
            # preset 模式下：若 preset 有有效 ref_wav，强制选 cosyvoice；
            # 否则走原有 _cosyvoice_reference_ready() 逻辑（检查全局 settings）。
            if preset_id and _ref_wav_ov and Path(_ref_wav_ov).exists():
                chosen = "cosyvoice"
            elif _cosyvoice_reference_ready():
                chosen = "cosyvoice"
            else:
                chosen = "edge-tts"
                logger.info("CosyVoice2 不可用（模型未加载/未配置参考音频），降级到 edge-tts")

        # 可用性前置检查（2026-09-10 任务005：日志谎称"将降级 edge-tts"实则 raise）。
        # 旧逻辑 preset 有 ref_wav 就强制 cosyvoice，_load_cosyvoice 导入失败
        # （No module named 'cosyvoice'）后直接 raise → 每镜重试 3 次 → 静音兜底。
        # 现在合成前先做一次廉价加载检查（带锁+缓存），不可用就真降级 edge-tts，
        # 片段照常有声（音色不同），并在结果里带 warning 供 pipeline 上浮。
        warn_note: Optional[str] = None
        if chosen == "cosyvoice":
            model = await asyncio.to_thread(_load_cosyvoice)
            if model is None:
                chosen = "edge-tts"
                warn_note = (
                    f"CosyVoice2 不可用（运行库缺失或模型加载失败），已降级 edge-tts "
                    f"音色 {settings.tts_fallback_voice}；preset={preset_id or '无'} 的原声未生效"
                )
                logger.warning("TTS 降级: %s", warn_note)

        # 输出文件名纳入完整身份（text+engine+voice+speed+preset+ref），
        # 避免「相同文案 + 不同音色/参考音」撞同名文件导致自动选声切换失效；
        # 同名即同身份，可直接复用缓存。
        ext = "wav" if chosen == "cosyvoice" else "mp3"
        _eff_ref = _ref_wav_ov or (getattr(settings, "cosyvoice_prompt_wav", "") if chosen == "cosyvoice" else "")
        _key = f"{text}|{chosen}|{voice}|{speed}|{preset_id or ''}|{_eff_ref}"
        filename = f"tts_{hashlib.md5(_key.encode('utf-8')).hexdigest()[:8]}.{ext}"
        filepath = OUTPUT_DIR / filename
        if filepath.exists():
            logger.info("TTS 缓存命中: %s", filename)
            return {
                "success": True, "engine": chosen,
                "audio_url": f"/api/tts/audio/{filename}",
                "audio_path": str(filepath), "duration_ms": 0, "error": None,
            }

        if chosen == "cosyvoice":
            # CosyVoice2 加载模型 + 推理为 CPU 重操作（首次约 14s 加载 + 逐句合成），
            # 放到线程池执行，避免阻塞事件循环（edge-tts 等其他请求/健康检查仍可响应）。
            audio_data, duration_ms = await asyncio.to_thread(
                tts_cosyvoice, text, speed,
                ref_wav_override=_ref_wav_ov,
                prompt_text_override=_prompt_text_ov,
            )
            content_type = "audio/wav"
        else:
            eff_voice = settings.tts_fallback_voice if warn_note else voice
            audio_data, duration_ms = await tts_edge(text, eff_voice, speed)
            content_type = "audio/mpeg"

        filepath.write_bytes(audio_data)

        logger.info(f"TTS 完成: engine={chosen}, bytes={len(audio_data)}")
        return {
            "success": True,
            "engine": chosen,
            "audio_url": f"/api/tts/audio/{filename}",
            "audio_path": str(filepath),
            "duration_ms": duration_ms,
            "error": None,
            "warning": warn_note,
        }

    except Exception as e:
        logger.error(f"TTS 失败: {e}")
        return {
            "success": False,
            "engine": engine,
            "audio_url": None,
            "audio_path": None,
            "duration_ms": None,
            "error": str(e),
        }


def health() -> dict:
    """TTS 健康状态（进程内始终可用；报告 CosyVoice2 是否就绪）。"""
    # 与 _load_cosyvoice() 对齐：仅以模型目录是否存在作为就绪判据，
    # 避免对 config 文件名（cosyvoice.yaml / config.yaml 等）做硬假设导致误报。
    cosy_ready = MODEL_DIR.exists()
    return {
        "status": "ok",
        "engine": "in-process",
        "cosyvoice_available": cosy_ready,
        "model_dir": str(MODEL_DIR),
    }


async def list_voices() -> list[dict]:
    """列出可用 edge-tts 音色（zh-）。"""
    import edge_tts

    voices = await edge_tts.list_voices()
    zh = [v for v in voices if v["Locale"].startswith("zh-")]
    return [
        {
            "id": v["ShortName"],
            "name": v["FriendlyName"],
            "locale": v["Locale"],
            "gender": v["Gender"],
        }
        for v in zh
    ]


def read_audio(filename: str) -> tuple[bytes, str]:
    """读取已生成的音频文件，返回 (bytes, media_type)。"""
    filepath = OUTPUT_DIR / filename
    if not filepath.exists():
        raise FileNotFoundError(f"音频文件不存在: {filename}")
    media_type = "audio/wav" if filename.endswith(".wav") else "audio/mpeg"
    return filepath.read_bytes(), media_type
