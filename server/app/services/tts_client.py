"""TTS 客户端 - 进程内调用 tts_core（合并后不再经 HTTP）。

保留与原 TTSClient 相同接口（generate / health_check / list_voices），
pipeline.py 调用方式不变；generate 额外返回 audio_path（本地产物绝对路径），
pipeline 直接落盘，省去原先从 TTS 服务再 HTTP 拉回的回环。
"""
import logging
from pathlib import Path
from typing import Optional

import httpx

from app.config import settings
from app.services import tts_core

logger = logging.getLogger(__name__)


class TTSClient:
    """TTS 客户端（进程内实现）。"""

    def __init__(self):
        # 合并后 TTS 在主进程内；base_url 仅用于把相对 audio_url 拼成绝对 URL（兜底兼容）。
        self.base_url = settings.server_public_url.rstrip("/")

    async def generate(
        self,
        text: str,
        voice: str = "zh-CN-YunxiNeural",
        speed: float = 1.0,
        engine: str = "auto",
        preset_id: Optional[str] = None,
    ) -> dict:
        """
        生成语音（进程内）。

        Args:
            preset_id: 可选的 voice preset id（从 voice_presets.json 解析 ref_wav+prompt_text）。

        Returns:
            {"success", "engine", "audio_url", "audio_path", "duration_ms", "error"}
        """
        try:
            return await tts_core.generate_tts(
                text=text, voice=voice, speed=speed, engine=engine,
                preset_id=preset_id,
            )
        except Exception as e:
            logger.error(f"TTS 调用失败: {e}")
            return {"success": False, "error": str(e)}

    async def health_check(self) -> bool:
        """进程内 TTS 始终可用（edge-tts 联网即可；CosyVoice2 缺失自动降级）。"""
        return True

    async def list_voices(self) -> list[dict]:
        """列出可用 edge-tts 音色。"""
        try:
            return await tts_core.list_voices()
        except Exception as e:
            logger.error(f"获取音色列表失败: {e}")
            return []

    async def list_presets(self) -> list[dict]:
        """列出可用 voice preset（含 ref_wav 就绪状态）。"""
        try:
            return tts_core.list_presets()
        except Exception as e:
            logger.error(f"获取 voice preset 列表失败: {e}")
            return []


# 全局客户端实例
tts_client = TTSClient()
