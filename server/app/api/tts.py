"""TTS API 路由（合并进主后端，前缀 /api/tts）。

与原独立 tts-server 的契约对齐：
  POST /api/tts/tts     生成语音
  GET  /api/tts/voices  列出 zh- 音色
  GET  /api/tts/audio/{filename}  获取音频文件
  GET  /api/tts/health   健康（含 CosyVoice2 是否就绪）
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional

from app.services import tts_core

router = APIRouter(tags=["TTS"])


class TTSRequest(BaseModel):
    text: str
    voice: str = "zh-CN-YunxiNeural"
    speed: float = 1.0
    engine: str = "auto"  # auto | cosyvoice | edge-tts
    preset_id: Optional[str] = None  # voice preset id（覆盖全局 ref_wav）


class TTSResponse(BaseModel):
    success: bool
    engine: str
    audio_url: Optional[str] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None


@router.post("/tts", response_model=TTSResponse)
async def generate_tts(req: TTSRequest):
    """生成语音（进程内，默认 edge-tts，CosyVoice2 就绪时 auto 优先）。

    可选 preset_id：从 voice_presets.json 解析 ref_wav+prompt_text 覆盖全局设置。
    """
    result = await tts_core.generate_tts(
        text=req.text, voice=req.voice, speed=req.speed,
        engine=req.engine, preset_id=req.preset_id
    )
    return TTSResponse(
        success=result["success"],
        engine=result["engine"],
        audio_url=result.get("audio_url"),
        duration_ms=result.get("duration_ms"),
        error=result.get("error"),
    )


@router.get("/voices")
async def list_voices():
    """列出可用的 edge-tts 中文音色。"""
    return {"voices": await tts_core.list_voices()}


@router.get("/presets")
async def list_presets():
    """列出可用的 voice preset（含 ref_wav 是否就绪）。"""
    return {"presets": tts_core.list_presets()}


@router.get("/audio/{filename}")
async def get_audio(filename: str):
    """获取生成的音频文件。"""
    try:
        data, media_type = tts_core.read_audio(filename)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="音频文件不存在")
    return Response(content=data, media_type=media_type)


@router.get("/health")
async def health():
    """TTS 健康状态。"""
    return tts_core.health()
