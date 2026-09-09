"""配置持久化服务

把页面/接口上的可配置项序列化到 ``system_settings`` 表，并支持：
- 启动时覆盖 Pydantic 内存里的默认（env-first, DB-overlay）
- 写入时同步内存 + DB（热更新生效）
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.models.system_setting import SystemSetting

logger = logging.getLogger(__name__)

# 写库白名单（只持久化这些键，避免脏字段污染）
WRITABLE_KEYS: tuple[str, ...] = (
    # 文本
    "text_api_key", "text_base_url", "text_model", "text_concurrency",
    "text_rpm",
    # 图片
    "image_api_key", "image_base_url", "image_model", "image_concurrency",
    "image_1k_rpm", "image_high_rpm",
    # 视频
    "video_api_key", "video_base_url", "video_model", "video_concurrency",
    "video_rpm", "enable_agnes_video",
    # 热点源
    "newsnow_base_url",
    # 通用
    "critic_concurrency", "script_score_threshold",
    "image_score_threshold", "max_retries",
    # TTS / 字幕（独立服务/资源）
    "tts_concurrency", "subtitle_concurrency",
    # 多平台输出（设置页"发布平台"多选，默认全选；渲染阶段实际使用的平台集合）
    "output_platforms",
    # 水印配置（设置页"水印配置"区块；流水线分镜图/视频实际叠加）
    "watermark_enabled", "watermark_text", "watermark_fontsize_ratio",
    "watermark_colour", "watermark_alpha", "watermark_margin_ratio",
    "watermark_position",
)


async def load_config_from_db() -> dict[str, Any]:
    """从 DB 读取持久化的配置（不存在则返回空 dict）。"""
    async with async_session_factory() as session:
        row = await session.scalar(
            select(SystemSetting).where(SystemSetting.scope == "singleton")
        )
        if not row or not row.config_json:
            return {}
        try:
            return json.loads(row.config_json)
        except (TypeError, ValueError) as exc:
            logger.warning("settings JSON 损坏，按空配置处理: %s", exc)
            return {}


async def save_config_to_db(values: dict[str, Any]) -> dict[str, Any]:
    """把可写白名单里的字段写入 DB，返回实际入库的子集。"""
    payload = {k: v for k, v in values.items() if k in WRITABLE_KEYS}
    async with async_session_factory() as session:
        row = await session.scalar(
            select(SystemSetting).where(SystemSetting.scope == "singleton")
        )
        if row is None:
            row = SystemSetting(scope="singleton", config_json=json.dumps(payload, ensure_ascii=False))
            session.add(row)
        else:
            # 与已有 JSON 合并，避免覆盖并发保存
            try:
                merged = json.loads(row.config_json or "{}")
            except (TypeError, ValueError):
                merged = {}
            merged.update(payload)
            row.config_json = json.dumps(merged, ensure_ascii=False)
            row.version = (row.version or 0) + 1
        await session.commit()
    return payload


async def reset_config_in_db() -> None:
    """清空持久化配置（恢复到 Pydantic 默认）。"""
    async with async_session_factory() as session:
        row = await session.scalar(
            select(SystemSetting).where(SystemSetting.scope == "singleton")
        )
        if row is not None:
            row.config_json = "{}"
            row.version = (row.version or 0) + 1
            await session.commit()


def apply_overlay(settings_obj: Any, overlay: dict[str, Any]) -> None:
    """把 DB 中的字段覆盖到 Pydantic Settings 实例上（仅对白名单生效）。"""
    for key, value in overlay.items():
        if key not in WRITABLE_KEYS:
            continue
        try:
            # 容忍前端把字符串数字塞进来
            setattr(settings_obj, key, value)
        except Exception as exc:  # pydantic ValidationError 等
            logger.warning("忽略非法设置 %s=%r: %s", key, value, exc)
