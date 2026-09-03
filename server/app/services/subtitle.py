"""字幕服务 - SRT 生成 + FFmpeg 烧录"""
import re
import logging
import subprocess
from pathlib import Path
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

# 字幕风格预设
SUBTITLE_STYLES = {
    "default": {
        "font": "Microsoft YaHei",
        "fontsize": 28,
        "primary_colour": "&H00FFFFFF",  # 白色
        "outline_colour": "&H00000000",  # 黑色
        "outline": 2,
        "shadow": 1,
        "alignment": 2,  # 底部居中
        "margin_v": 30,
    },
    "kai": {
        # 古风楷体：契合诗词意境；靠 2px 黑描边 + 阴影保小屏可读
        "font": "KaiTi",
        "fontsize": 40,
        "primary_colour": "&H00FFFFFF",  # 白色
        "outline_colour": "&H00000000",  # 黑色
        "outline": 2,
        "shadow": 1,
        "alignment": 2,  # 底部居中
        "margin_v": 80,
    },
    "yahei": {
        # 安全黑体：最大可读性，通用
        "font": "Microsoft YaHei",
        "fontsize": 38,
        "primary_colour": "&H00FFFFFF",
        "outline_colour": "&H00000000",
        "outline": 2,
        "shadow": 1,
        "alignment": 2,
        "margin_v": 70,
    },
    "ancient": {
        "font": "KaiTi",
        "fontsize": 30,
        "primary_colour": "&H00FFFFFF",
        "outline_colour": "&H00000000",
        "outline": 2,
        "shadow": 1,
        "alignment": 2,
        "margin_v": 30,
    },
    "modern": {
        "font": "SimHei",
        "fontsize": 26,
        "primary_colour": "&H00FFFFFF",
        "outline_colour": "&H00000000",
        "outline": 2,
        "shadow": 1,
        "alignment": 2,
        "margin_v": 30,
    },
    "emotion": {
        "font": "SimSun",
        "fontsize": 26,
        "primary_colour": "&H00E8E8D0",  # 淡黄色
        "outline_colour": "&H00000000",
        "outline": 1,
        "shadow": 1,
        "alignment": 2,
        "margin_v": 30,
    },
}


def split_text_to_sentences(text: str, max_chars: int = 20) -> list[str]:
    """
    将文案按标点分句
    
    Args:
        text: 原始文案
        max_chars: 每句最大字符数
        
    Returns:
        分句列表
    """
    # 先按标点分割
    sentences = re.split(r'[。！？，；：、\n]+', text)
    sentences = [s.strip() for s in sentences if s.strip()]
    
    # 合并过短的句子
    merged = []
    buffer = ""
    for s in sentences:
        if len(buffer) + len(s) < max_chars:
            buffer += s
        else:
            if buffer:
                merged.append(buffer)
            buffer = s
    if buffer:
        merged.append(buffer)
    
    return merged


def text_to_srt(
    text: str,
    duration_ms: int,
    max_chars_per_line: int = 20,
) -> str:
    """
    将文案转换为 SRT 字幕
    
    Args:
        text: 文案文本
        duration_ms: 总时长（毫秒）
        max_chars_per_line: 每行最大字符数
        
    Returns:
        SRT 格式字符串
    """
    sentences = split_text_to_sentences(text, max_chars_per_line)
    if not sentences:
        return ""
    
    # 均匀分配时间
    interval = duration_ms // len(sentences)
    
    srt_lines = []
    for i, sentence in enumerate(sentences):
        start_ms = i * interval
        end_ms = min((i + 1) * interval, duration_ms)
        
        start = _ms_to_srt_time(start_ms)
        end = _ms_to_srt_time(end_ms)
        
        srt_lines.append(f"{i + 1}")
        srt_lines.append(f"{start} --> {end}")
        srt_lines.append(sentence)
        srt_lines.append("")
    
    return "\n".join(srt_lines)


def _ms_to_srt_time(ms: int) -> str:
    """毫秒转 SRT 时间格式"""
    hours = ms // 3600000
    minutes = (ms % 3600000) // 60000
    seconds = (ms % 60000) // 1000
    millis = ms % 1000
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def burn_subtitles(
    video_path: str,
    srt_path: str,
    output_path: str,
    style_name: str = "default",
) -> bool:
    """
    使用 FFmpeg 将字幕烧录到视频
    
    Args:
        video_path: 输入视频路径
        srt_path: SRT 字幕文件路径
        output_path: 输出视频路径
        style_name: 字幕风格预设
        
    Returns:
        是否成功
    """
    style = SUBTITLE_STYLES.get(style_name, SUBTITLE_STYLES["default"])
    
    # 构建 FFmpeg 字幕样式
    force_style = (
        f"FontName={style['font']},"
        f"FontSize={style['fontsize']},"
        f"PrimaryColour={style['primary_colour']},"
        f"OutlineColour={style['outline_colour']},"
        f"Outline={style['outline']},"
        f"Shadow={style['shadow']},"
        f"Alignment={style['alignment']},"
        f"MarginV={style['margin_v']}"
    )
    
    cmd = [
        settings.ffmpeg_path,
        "-y",
        "-i", video_path,
        "-vf", f"subtitles={srt_path}:force_style='{force_style}'",
        "-c:a", "copy",
        output_path,
    ]
    
    logger.info(f"FFmpeg 烧录字幕: {' '.join(cmd[:5])}...")
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
        
        if result.returncode != 0:
            logger.error(f"FFmpeg 失败: {result.stderr}")
            return False
        
        logger.info(f"字幕烧录完成: {output_path}")
        return True
        
    except subprocess.TimeoutExpired:
        logger.error("FFmpeg 执行超时")
        return False
    except FileNotFoundError:
        logger.error("FFmpeg 未安装或路径错误")
        return False


def generate_subtitle_file(
    text: str,
    duration_ms: int,
    output_path: str,
    max_chars_per_line: int = 20,
) -> str:
    """
    生成 SRT 字幕文件
    
    Args:
        text: 文案文本
        duration_ms: 总时长（毫秒）
        output_path: 输出 SRT 文件路径
        max_chars_per_line: 每行最大字符数
        
    Returns:
        SRT 文件路径
    """
    srt_content = text_to_srt(text, duration_ms, max_chars_per_line)
    
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(srt_content, encoding="utf-8")
    
    logger.info(f"SRT 字幕生成: {output_path} ({len(srt_content)} chars)")
    return output_path
