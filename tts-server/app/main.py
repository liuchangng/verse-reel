"""TTS 服务 - CosyVoice2 本地 + edge-tts 降级"""
import os
import io
import logging
import tempfile
from pathlib import Path
from typing import Optional

import edge_tts
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tts-server")

app = FastAPI(title="TTS Server", version="0.1.0")

#CosyVoice2 模型（延迟加载）
_cosyvoice_model = None
_cosyvoice_available = False

# 输出目录
OUTPUT_DIR = Path("outputs/audio")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


class TTSRequest(BaseModel):
    """TTS 请求"""
    text: str
    voice: str = "zh-CN-YunxiNeural"  # edge-tts 音色
    speed: float = 1.0  # 语速
    engine: str = "auto"  # auto | cosyvoice | edge-tts


class TTSResponse(BaseModel):
    """TTS 响应"""
    success: bool
    engine: str
    audio_url: Optional[str] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None


def _load_cosyvoice():
    """延迟加载 CosyVoice2 模型"""
    global _cosyvoice_model, _cosyvoice_available
    
    if _cosyvoice_model is not None:
        return _cosyvoice_model
    
    try:
        import torch
        from cosyvoice.cli.cosyvoice import CosyVoice2
        
        model_dir = Path("pretrained_models/CosyVoice2-0.5B")
        if not model_dir.exists():
            logger.warning(f"CosyVoice2 模型目录不存在: {model_dir}")
            _cosyvoice_available = False
            return None
        
        logger.info("加载 CosyVoice2 模型...")
        _cosyvoice_model = CosyVoice2(str(model_dir))
        _cosyvoice_available = True
        logger.info("CosyVoice2 模型加载完成")
        return _cosyvoice_model
        
    except Exception as e:
        logger.error(f"CosyVoice2 加载失败: {e}")
        _cosyvoice_available = False
        return None


async def tts_edge(text: str, voice: str, speed: float) -> tuple[bytes, int]:
    """使用 edge-tts 生成语音"""
    communicate = edge_tts.Communicate(text, voice, rate=f"+{int((speed-1)*100)}%")
    
    audio_data = b""
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_data += chunk["data"]
    
    return audio_data, len(audio_data)


def tts_cosyvoice(text: str, speed: float = 1.0) -> tuple[bytes, int]:
    """使用 CosyVoice2 生成语音"""
    model = _load_cosyvoice()
    if model is None:
        raise RuntimeError("CosyVoice2 模型未加载")
    
    import numpy as np
    import soundfile as sf
    
    # 生成语音
    output = model.inference_sft(text, "中文女")
    
    # 提取音频数据
    audio_chunks = []
    for chunk in output:
        audio_chunks.append(chunk["tts_speech"])
    
    audio = np.concatenate(audio_chunks, axis=0)
    
    # 写入 WAV
    buf = io.BytesIO()
    sf.write(buf, audio.squeeze(), 22050, format="WAV")
    buf.seek(0)
    
    return buf.read(), len(audio.squeeze()) * 1000 // 22050


@app.get("/health")
async def health():
    """健康检查"""
    return {
        "status": "ok",
        "cosyvoice_available": _cosyvoice_available,
    }


@app.post("/tts", response_model=TTSResponse)
async def generate_tts(request: TTSRequest):
    """生成语音"""
    try:
        # 确定使用哪个引擎
        engine = request.engine
        if engine == "auto":
            # 尝试 CosyVoice2，失败则降级到 edge-tts
            if _cosyvoice_available or _load_cosyvoice() is not None:
                engine = "cosyvoice"
            else:
                engine = "edge-tts"
                logger.info("CosyVoice2 不可用，降级到 edge-tts")
        
        if engine == "cosyvoice":
            audio_data, duration_ms = tts_cosyvoice(request.text, request.speed)
            content_type = "audio/wav"
        else:
            audio_data, duration_ms = await tts_edge(
                request.text, request.voice, request.speed
            )
            content_type = "audio/mpeg"
        
        # 保存文件
        ext = "wav" if engine == "cosyvoice" else "mp3"
        filename = f"tts_{hash(request.text) & 0xFFFFFFFF:08x}.{ext}"
        filepath = OUTPUT_DIR / filename
        filepath.write_bytes(audio_data)
        
        logger.info(f"TTS 完成: engine={engine}, duration={duration_ms}ms")
        
        return TTSResponse(
            success=True,
            engine=engine,
            audio_url=f"/audio/{filename}",
            duration_ms=duration_ms,
        )
        
    except Exception as e:
        logger.error(f"TTS 失败: {e}")
        return TTSResponse(
            success=False,
            engine=request.engine,
            error=str(e),
        )


@app.get("/audio/{filename}")
async def get_audio(filename: str):
    """获取生成的音频文件"""
    filepath = OUTPUT_DIR / filename
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="音频文件不存在")
    
    if filename.endswith(".wav"):
        media_type = "audio/wav"
    else:
        media_type = "audio/mpeg"
    
    return Response(
        content=filepath.read_bytes(),
        media_type=media_type,
    )


@app.get("/voices")
async def list_voices():
    """列出可用的 edge-tts 音色"""
    voices = await edge_tts.list_voices()
    zh_voices = [v for v in voices if v["Locale"].startswith("zh-")]
    return {
        "voices": [
            {
                "id": v["ShortName"],
                "name": v["FriendlyName"],
                "locale": v["Locale"],
                "gender": v["Gender"],
            }
            for v in zh_voices
        ]
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8003)
