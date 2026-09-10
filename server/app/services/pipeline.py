"""流水线引擎 - 协调文案/图片/视频/配音/字幕生成"""
import json
import re
import logging
import asyncio
import subprocess
import httpx
from pathlib import Path
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.poem import Poem
from app.models.task import Task
from app.models.script import Script
from app.services.agnes import agnes_client
from app.services.critic import critic_service, ScoreResult
from app.services.tts_client import tts_client
from app.services.subtitle import (
    generate_subtitle_file,
    burn_subtitles,
    SUBTITLE_STYLES,
)
from app.services.hotspot import hotspot_service
from app.services.prompt_optimizer import prompt_optimizer
from app.services.character import character_service
from app.services.publisher import publisher_service
from app.config import settings, tier_of, resolve_task_tier, tier_script_guidelines, tier_profile

logger = logging.getLogger(__name__)


def _extract_video_url(result: dict) -> str:
    """从 agnes 视频轮询结果中兼容多种结构提取视频 URL。

    agnes 不同模型/版本的返回字段位置不稳定，常见路径：
    - result["metadata"]["url"]
    - result["url"] / result["video_url"]
    - result["data"]["url"]
    - result["outputs"][0] / result["output"]["url"]
    """
    if not isinstance(result, dict):
        return ""

    # 路径1: metadata.url（最常见）
    meta = result.get("metadata")
    if isinstance(meta, dict) and meta.get("url"):
        return meta["url"]

    # 路径2: 顶层 url / video_url
    for key in ("url", "video_url", "download_url", "play_url"):
        if isinstance(result.get(key), str) and result[key]:
            return result[key]

    # 路径3: data.url
    data = result.get("data")
    if isinstance(data, dict) and data.get("url"):
        return data["url"]
    if isinstance(data, str) and data.startswith("http"):
        return data

    # 路径4: outputs / output 列表或对象
    outputs = result.get("outputs") or result.get("output")
    if isinstance(outputs, list) and outputs:
        first = outputs[0]
        if isinstance(first, str) and first.startswith("http"):
            return first
        if isinstance(first, dict):
            for k in ("url", "video_url", "play_url"):
                if first.get(k):
                    return first[k]
    if isinstance(outputs, dict) and outputs.get("url"):
        return outputs["url"]

    return ""


# 平台 → 视频尺寸映射
# 注：比例分组决定「去重渲染」粒度（见 run_pipeline 字幕阶段）：
#   9:16 → 抖音/快手（共享一份 base）
#   16:9 → B站/YouTube（共享一份 base）
#   3:4  → 小红书（独立）
# 故 5 个平台仅需要 ≤3 种分辨率渲染。
# duration_tier: 平台所属档位（S 快档/L 深档，见 config.TIER_PROFILES），
#   单一事实源在 config.PLATFORM_TIER——文案/分镜档位化（W2-W4）与成片
#   时长校验（W5）据此决定用哪套预算，消除 §一 口径脱节（假默认 30）。
PLATFORM_CONFIG = {
    "douyin": {"aspect_ratio": "9:16", "size": "720P", "label": "抖音", "duration_tier": tier_of("douyin")},
    "xiaohongshu": {"aspect_ratio": "3:4", "size": "720P", "label": "小红书", "duration_tier": tier_of("xiaohongshu")},
    "kuaishou": {"aspect_ratio": "9:16", "size": "720P", "label": "快手", "duration_tier": tier_of("kuaishou")},
    "bilibili": {"aspect_ratio": "16:9", "size": "720P", "label": "B站", "duration_tier": tier_of("bilibili")},
    "youtube": {"aspect_ratio": "16:9", "size": "720P", "label": "YouTube", "duration_tier": tier_of("youtube")},
}

# 视频宽高映射（agnes-video-v2.0 要求 width/height，按平台比例推导）
ASPECT_TO_WH = {
    "9:16": (768, 1152),
    "16:9": (1152, 768),
    "3:4": (864, 1152),
    "4:3": (1024, 768),
    "1:1": (1024, 1024),
}

# 最终合成分辨率（按平台比例，1080 宽基准竖屏；与 agnes 视频生成尺寸解耦，
# 是「逐镜以图定音」幻灯片真正输出的画幅）
FINAL_RESOLUTION = {
    "9:16": (1080, 1920),   # 抖音 / 快手
    "16:9": (1920, 1080),   # B站横屏
    "3:4": (1080, 1440),    # 小红书
    "4:3": (1440, 1080),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
}


def final_resolution(platform: str) -> tuple[int, int]:
    """最终合成视频分辨率（W, H），按平台比例推导；缺省抖音 9:16。"""
    cfg = PLATFORM_CONFIG.get(platform, PLATFORM_CONFIG["douyin"])
    return FINAL_RESOLUTION.get(cfg["aspect_ratio"], (1080, 1920))


class PipelineEngine:
    """流水线引擎"""
    
    def __init__(self):
        self._video_semaphore = asyncio.Semaphore(settings.video_concurrency)
    
    async def create_task(
        self,
        db: AsyncSession,
        poem_id: int,
        platform: str = "douyin",
        platforms: list[str] | None = None,
        source_hotspot_title: str | None = None,
        source_keywords: list[str] | None = None,
    ) -> Task:
        """
        创建新任务

        Args:
            db: 数据库会话
            poem_id: 诗词 ID
            platform: 目标平台
            source_hotspot_title: 来源热点标题（热点页创建时传入；诗词库创建为空）
            source_keywords: 来源热点关键词（同上；为空 = 非热点任务，文案不注入热词）

        Returns:
            创建的任务
        """
        # 检查诗词是否存在
        poem = await db.get(Poem, poem_id)
        if not poem:
            raise ValueError(f"诗词不存在: {poem_id}")

        # 创建任务
        # platforms: 本任务显式选中的发布平台（来自创建弹窗多选）；为空则留空，
        # 渲染阶段回退到全局 settings.output_platforms。
        _platforms = platforms or []
        # 热点任务：创建时即按来源热点主题定风格（旧版在文案阶段实时抓全榜推断，
        # 时过境迁后会漂移到与任务无关的热点上）。
        derived_style = None
        if source_hotspot_title:
            try:
                themes = hotspot_service.match_themes(
                    {"source": [{"title": source_hotspot_title}]}
                )
                derived_style = self._select_style(themes)
            except Exception as exc:  # 主题匹配失败不阻断创建
                logger.warning("来源热点主题匹配失败(热点=%r): %s", source_hotspot_title, exc)
                derived_style = None
        task = Task(
            poem_id=poem_id,
            status="pending",
            platform=platform,
            platforms=json.dumps(_platforms, ensure_ascii=False) if _platforms else None,
            source_hotspot_title=source_hotspot_title,
            source_keywords=json.dumps(source_keywords, ensure_ascii=False) if source_keywords else None,
            style=derived_style,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        
        logger.info(f"创建任务: {task.id} - {poem.title}")
        return task
    
    def _select_style(self, themes: list[str]) -> str:
        """根据主题选择风格"""
        # 主题→风格映射
        theme_style_map = {
            "失意": "职场共鸣",
            "怀才不遇": "职场共鸣",
            "壮志难酬": "职场共鸣",
            "爱情": "情感治愈",
            "相思": "情感治愈",
            "离别": "情感治愈",
            "历史": "历史解读",
            "豪放": "历史解读",
            "边塞": "历史解读",
            "哲理": "人生感悟",
            "人生感悟": "人生感悟",
            "田园": "人生感悟",
        }
        
        # 按优先级匹配
        for theme in themes:
            if theme in theme_style_map:
                return theme_style_map[theme]

        return "人生感悟"  # 默认

    # ------------------------------------------------------------------ #
    # 单阶段执行（队列调用入口）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_json_list(raw) -> list:
        """把 JSON 字符串/list/None 统一解析为 list（task 字段可能三种形态）。"""
        if not raw:
            return []
        if isinstance(raw, list):
            return raw
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []

    async def run_stage(
        self,
        db: AsyncSession,
        task_id: int,
        stage: str,
        force: bool = False,
    ) -> None:
        """队列 worker 调用入口：**只执行指定阶段**，幂等且互不牵连。

        设计要点
        --------
        - 每个阶段直接调用对应的内部方法（``_generate_script`` /
          ``_generate_images`` / ``_generate_tts`` / ``_generate_video`` /
          ``_burn_subtitles`` / ``character_service``），**不再委托
          ``run_pipeline``** —— 后者是无自检的全流程，委托它会导致
          "只重生成图片" 变成 "重跑全部六个阶段"。
        - 产物自检（``_have``）：产物已存在则跳过，保证重试/恢复安全。
        - ``force=True``：忽略已有产物强制重跑。配合队列侧「按阶段范围
          清空产物」使用，即可实现「只重生成图片，保留定妆照与文案」。

        Args:
            db: 数据库会话
            task_id: 任务 ID
            stage: script / character / image / tts / video / subtitle
            force: 是否忽略已有产物强制重跑
        """
        task = await db.get(Task, task_id)
        if not task:
            raise ValueError(f"任务不存在: {task_id}")
        poem = await db.get(Poem, task.poem_id) if task.poem_id else None
        style = task.style or "人生感悟"

        def _have(field: str) -> bool:
            v = getattr(task, field, None)
            return bool(v) and (
                not isinstance(v, str) or v.strip() not in ("", "[]", "null", "{}")
            )

        if stage == "script":
            if _have("script") and not force:
                logger.info(f"task{task_id} script 已存在，跳过 stage")
                return
            if poem is None:
                raise RuntimeError("script 缺少前置 poem")
            # 热点来源修复（2026-09-09）：文案只注入"任务创建时持久化的来源热点"。
            # 诗词库创建的任务 source_keywords 为空 → 不注入任何热词。
            # 旧版此处实时重抓全平台热榜 Top3 注入，生成时刻的热门新闻（如
            # "苹果折叠屏"）被硬揉进文案，把内容污染得乱七八糟。
            keywords = task.source_keywords_list
            if keywords:
                logger.info(
                    f"task{task_id} 注入来源热点: {task.source_hotspot_title!r}, "
                    f"keywords={keywords[:3]}..."
                )
            script_text, score = await self._generate_script(db, task, poem, style, keywords)
            task.style = style
            if not score.passed:
                task.status = "failed"
                task.error_message = f"文案评分未达标: {score.feedback}"
                await db.commit()
                return
            # 文案变了 → 下游产物全部失效，清空让后续阶段重跑。
            # 2026-09-09 任务001事故：旧版只清图片侧，残留的旧音频/旧视频既造成
            # 前端"下游已完成"的乱序假象，也会被产物自检复用（旧片配新文案，内容
            # 彻底脱节）。文案是全链源头，重跑即全链失效。
            task.storyboard = None
            task.character_ref = None
            task.image_urls = None
            task.image_score = None
            task.audio_url = None
            task.video_url = None
            task.video_duration = None
            task.subtitle_url = None
            await db.commit()

            # 分镜（storyboard）是 script 阶段的产物之一（STAGE_OUTPUTS["script"]
            # 含 storyboard；run_pipeline 阶段2 同源逻辑）。2026-09-09 事故：
            # 旧版此处只清空不生成——分镜生成只存在于 run_pipeline 直跑路径，
            # 创建任务统一入队后 storyboard 永远为空，image/tts/subtitle 三个
            # 阶段必然"缺少前置 storyboard"失败。
            task.current_stage = "storyboard"
            await db.commit()
            sb_tier = resolve_task_tier(self._task_platforms(task))
            storyboard = await self._generate_storyboard(script_text, tier=sb_tier)
            task.storyboard = json.dumps(storyboard, ensure_ascii=False)
            await db.commit()
            logger.info(f"task{task_id} 分镜生成完成: {len(storyboard)} 镜 (tier={sb_tier})")

        elif stage == "character":
            if _have("character_ref") and not force:
                logger.info(f"task{task_id} character 已存在，跳过 stage")
                return
            if not _have("script"):
                raise RuntimeError("character 缺少前置 script")
            # S4：返回 {ref, description, name}。description（六层特征锚点）回灌
            # 提示词 + ref（四视图定妆照）做 i2i 参考，双保险一致性。
            result = await character_service.generate_character_reference(task.script, style)
            task.character_ref = result.get("ref")
            task.character_description = result.get("description")
            await db.commit()
            if task.character_ref:
                logger.info(
                    f"task{task_id} 角色定妆照生成完成: {task.character_ref}"
                    f" | 特征锚点: {task.character_description}"
                )
            else:
                # fail-loud：定妆照是后续 6 张分镜图一致性的锚点，缺失时
                # 不应静默继续生成互不一致的分镜图，直接抛错让任务失败可见。
                task.status = "failed"
                task.error_message = "角色定妆照生成失败（一致性锚点缺失），已终止以免产出不一致分镜"
                await db.commit()
                raise RuntimeError(task.error_message)

        elif stage == "image":
            if _have("image_urls") and not force:
                logger.info(f"task{task_id} image 已存在，跳过 stage")
                return
            storyboard = self._parse_json_list(task.storyboard)
            if not storyboard:
                raise RuntimeError("image 缺少前置 storyboard")
            image_urls = await self._generate_images(
                task, storyboard, db,
                character_ref=task.character_ref,
                style=style,
                character_description=task.character_description,
            )
            if image_urls:
                task.image_urls = json.dumps(image_urls, ensure_ascii=False)
                if not task.image_score:
                    task.image_score = 8
            await db.commit()
            logger.info(f"task{task_id} image 阶段完成: {len(image_urls)} 张")

        elif stage == "tts":
            if _have("audio_url") and not force:
                logger.info(f"task{task_id} tts 已存在，跳过 stage")
                return
            storyboard = self._parse_json_list(task.storyboard)
            if not storyboard:
                raise RuntimeError("tts 缺少前置 storyboard")
            tts_segments = await self._generate_tts_segments(task, storyboard, db, force=force)
            if not tts_segments.get("success"):
                raise RuntimeError(f"tts 生成失败: {tts_segments.get('error')}")
            await db.commit()
            logger.info(f"task{task_id} tts 阶段完成")

        elif stage == "video":
            if _have("video_url") and not force:
                logger.info(f"task{task_id} video 已存在，跳过 stage")
                return
            image_urls = self._parse_json_list(task.image_urls)
            if not image_urls:
                # 2026-09-09 任务001事故（用户定夺）：移除"定妆照兜底"——旧版在
                # 分镜图缺失时拿定妆照凑一张图出片，视频与分镜/旁白彻底脱节，还
                # 掩盖了 image 阶段的失败。fail-loud：缺分镜图就失败，原因可见。
                raise RuntimeError("video 缺少前置 image 产物（分镜图为空），请先重跑图片生成阶段")
            platform_cfg = PLATFORM_CONFIG.get(task.platform, PLATFORM_CONFIG["douyin"])
            video_url = await self._generate_video(task, image_urls, platform_cfg, style)
            if video_url:
                task.video_url = video_url
                # 视频时长（秒）从平台配置回填，供字幕阶段估算
                task.video_duration = platform_cfg.get("duration_sec", 30)
                await db.commit()
                logger.info(f"task{task_id} video 阶段完成: {video_url}")
            else:
                raise RuntimeError("video 生成失败：未返回有效 URL")

        elif stage == "subtitle":
            if _have("subtitle_url") and not force:
                logger.info(f"task{task_id} subtitle 已存在，跳过 stage")
                return
            storyboard = self._parse_json_list(task.storyboard)
            if not storyboard:
                raise RuntimeError("subtitle 缺少前置 storyboard")
            # 1) 逐镜 TTS（若未生成则补生成；force 时重生成）
            tts_segments = await self._generate_tts_segments(task, storyboard, db, force=force)
            if not tts_segments.get("success"):
                raise RuntimeError(f"tts 生成失败: {tts_segments.get('error')}")
            # 2) 逐镜 seg 合成（base.mp4 含旁白音轨，时长=Σ旁白），按平台分辨率输出
            W, H = final_resolution(task.platform)
            segs = await self._build_segments(task, storyboard, tts_segments, W, H,
                                              platform=task.platform or "douyin")
            if not segs.get("ok"):
                raise RuntimeError("视频片段合成失败")
            # 3) 烧字幕 + BGM → final.mp4（彻底弃用 agnes 5s 片段音轨）
            subtitle_url = await self._burn_subtitles(task, segs["base"], storyboard, segs, W, H,
                                                     platform=task.platform or "douyin",
                                                     poem_content=poem.content if poem else None)
            if subtitle_url:
                task.subtitle_url = subtitle_url
                # 展示视频同样指向完整成片（覆盖 agnes 5s 片段）
                task.video_url = subtitle_url
                await db.commit()

        else:
            raise ValueError(f"未知阶段: {stage}")

        # 阶段完成后让 task.status 反映进度（pending -> processing -> pending_review）
        await db.refresh(task)
        logger.info(f"task{task_id} 阶段 {stage} 完成: status={task.status}")
    
    async def run_pipeline(
        self,
        db: AsyncSession,
        task_id: int,
    ):
        """
        执行完整流水线
        
        Args:
            db: 数据库会话
            task_id: 任务 ID
        """
        # 获取任务和诗词
        task = await db.get(Task, task_id)
        if not task:
            raise ValueError(f"任务不存在: {task_id}")
        
        poem = await db.get(Poem, task.poem_id)
        if not poem:
            raise ValueError(f"诗词不存在: {task.poem_id}")
        
        logger.info(f"开始执行流水线: 任务 {task_id} - {poem.title}")
        
        try:
            # 更新状态
            task.status = "processing"
            task.current_stage = "script"
            task.progress = 10
            await db.commit()
            
            # 阶段0: 热点来源（2026-09-09 修复）：不再实时抓全平台热榜。
            # 文案关键词只来自任务创建时持久化的来源热点；无来源（诗词库创建/
            # 存量任务）则不注入任何热词。风格同理，用创建时定好的 task.style。
            style = task.style or "人生感悟"
            keywords = task.source_keywords_list
            
            # 阶段1: 生成文案（使用风格化的提示词）
            script_text, script_score = await self._generate_script(
                db, task, poem, style, keywords
            )
            
            if not script_score.passed:
                task.status = "failed"
                task.error_message = f"文案评分未达标: {script_score.feedback}"
                await db.commit()
                return
            
            # 阶段2: 生成分镜（文案/分镜链统一按任务平台解析的档位走）
            task.current_stage = "storyboard"
            task.progress = 30
            await db.commit()

            sb_tier = resolve_task_tier(self._task_platforms(task))
            storyboard = await self._generate_storyboard(script_text, tier=sb_tier)
            task.storyboard = json.dumps(storyboard, ensure_ascii=False)
            await db.commit()
            
            # 阶段2.5: 生成角色定妆照
            # 注意：generate_character_reference 返回 dict {ref, description, name}，
            # 其中 ref 才是定妆照 URL（i2i 参考图），不可把整个 dict 直接写库。
            character_result = await character_service.generate_character_reference(
                script_text, style
            )
            character_ref = character_result.get("ref")
            # 持久化定妆照 URL 与角色描述，供前端展示、视频与分镜图复用
            task.character_ref = character_ref
            task.character_description = character_result.get("description")
            await db.commit()
            if character_ref:
                logger.info(f"角色定妆照生成完成: {character_ref}")

            # 阶段3: 生成图片 + 打分（使用角色参考图 + 角色描述注入一致性）
            task.current_stage = "image"
            task.progress = 40
            await db.commit()

            image_urls = await self._generate_images(
                task, storyboard, db,
                character_ref=character_ref,
                style=style,
                character_description=character_result.get("description"),
            )
            logger.info(f"生成图片完成: {len(image_urls)} 张")

            # 回写图片 URL 到任务
            if image_urls:
                task.image_urls = json.dumps(image_urls, ensure_ascii=False)
                # 取首张图片评分作为整体图片评分
                if not task.image_score:
                    task.image_score = 8  # 默认通过分
            await db.commit()
            
            # 阶段4: 生成视频（使用平台配置的尺寸）
            # agnes 片段在成片中被 final.mp4（图片+TTS 幻灯片）完全覆盖（见
            # _burn_subtitles 注释），默认关闭省去每任务 ≥1 分钟（1 次/分钟限制）
            # 与 API 费用；enable_agnes_video=true 或手动 regenerate stage=video 可启用。
            if not settings.enable_agnes_video:
                logger.info(f"task{task_id} agnes 视频片段已关闭(enable_agnes_video=False)，跳过阶段4")
            else:
                task.current_stage = "video"
                task.progress = 60
                await db.commit()

                platform_cfg = PLATFORM_CONFIG.get(task.platform, PLATFORM_CONFIG["douyin"])
                video_url = await self._generate_video(task, image_urls, platform_cfg, style)

                # 立即回写视频 URL
                task.video_url = video_url
                await db.commit()
            
            # 阶段5: TTS 配音
            task.current_stage = "tts"
            task.progress = 75
            await db.commit()
            
            tts_segments = await self._generate_tts_segments(task, storyboard, db)
            
            # 阶段6: 字幕烧录（多平台：TTS/图片/SRT 复用，按分辨率分别合成）
            task.current_stage = "subtitle"
            task.progress = 90
            await db.commit()

            # 本任务选中的平台优先；未指定则回退全局 settings.output_platforms（系统设置页配置）。
            # 与文案链 _generate_script 共用 _task_platforms 解析（口径一致）。
            platforms = self._task_platforms(task)
            platform_urls = {}
            # 按画幅比例分组：同比例平台只构建一次 base 视频，仅各自烧录 final_{plat}.mp4。
            # 例：抖音/快手(9:16) 共享一份 base，B站/YouTube(16:9) 共享一份 base，
            # 小红书(3:4) 独立 → 5 平台最多 3 种分辨率渲染，避免重复 ffmpeg 拼接。
            ar_groups: dict[str, list[str]] = {}
            for plat in platforms:
                ar = PLATFORM_CONFIG.get(plat, PLATFORM_CONFIG["douyin"])["aspect_ratio"]
                ar_groups.setdefault(ar, []).append(plat)
            for ar, plats in ar_groups.items():
                rep = plats[0]  # 组内代表平台（base 文件以其命名）
                W, H = final_resolution(rep)
                logger.info(f"task{task_id} 比例组 {ar}: 平台 {plats} → 构建一次 base({rep}) {W}x{H}")
                segs = await self._build_segments(task, storyboard, tts_segments, W, H,
                                                  platform=rep)
                if not segs.get("ok"):
                    logger.warning(f"task{task_id} 比例组 {ar} base 合成失败，跳过该组 {plats}")
                    continue
                for plat in plats:
                    final_url = await self._burn_subtitles(
                        task, segs["base"], storyboard, segs, W, H,
                        platform=plat, poem_content=poem.content,
                    )
                    if final_url:
                        platform_urls[plat] = final_url
                        logger.info(f"task{task_id} 平台 {plat}: ✅ {final_url}")
                    else:
                        logger.warning(f"task{task_id} 平台 {plat}: 烧录失败")
            
            # 完成自动阶段 → 进入待人工审核（Q2A：发布前审核）
            # 不再直接置 done，而是等人工 approve 后由 review 端点置 done
            # current_stage 保持在最后完成的阶段（subtitle），不回退，确保前端步骤条正确
            task.status = "pending_review"
            task.current_stage = "subtitle"  # 最后实际执行阶段
            task.progress = 95
            task.review_status = "pending"
            # 最终对外视频必须是同步旁白的完整幻灯片(final.mp4)，而非 agnes 5s 片段。
            # 多平台模式：主平台 URL 作为 video_url，其余存 platform_outputs JSON。
            primary_plat = task.platform or "douyin"
            if platform_urls:
                task.video_url = platform_urls.get(primary_plat,
                                                  list(platform_urls.values())[0])
                if len(platform_urls) > 1:
                    task.platform_outputs = json.dumps(platform_urls, ensure_ascii=False)
            await db.commit()
            
            logger.info(f"✅ 流水线待审核: 任务 {task_id} - {poem.title}（pending_review, 进度 95%）")
            
        except Exception as e:
            logger.error(f"流水线失败: {task_id} - {e}")
            task.status = "failed"
            task.error_message = str(e)
            await db.commit()
            raise
    
    @staticmethod
    def _task_platforms(task: Task) -> list[str]:
        """本任务显式发布平台（task.platforms JSON）；为空回退 settings.output_platforms。

        与渲染段共用同一解析（video-comm 决策 1：文案链按任务平台解析档位）。
        """
        raw = getattr(task, "platforms", None)
        if raw:
            try:
                plats = json.loads(raw) or []
                if plats:
                    return plats
            except (TypeError, ValueError):
                pass
        return list(getattr(settings, "output_platforms", None) or ["douyin"])

    async def _generate_script(
        self,
        db: AsyncSession,
        task: Task,
        poem: Poem,
        style: str = "人生感悟",
        keywords: list[str] | None = None,
    ) -> tuple[str, ScoreResult]:
        """
        生成并评分文案（带重试）
        
        Returns:
            (文案文本, 评分结果)
        """
        # video-comm 文案改造轮（W3）：档位段前置注入。tier 由任务平台解析
        # （本轮默认产出 douyin/kuaishou/xiaohongshu → S 快档三段式 80–130 字；
        # L 五段预案已随 config.TIER_SCRIPT_STRUCTURE 就位，扩展时自动生效）。
        task_plats = self._task_platforms(task)
        tier = resolve_task_tier(task_plats)
        logger.info(f"task{task.id} 文案档位解析: tier={tier} (platforms={task_plats})")

        for attempt in range(settings.max_retries):
            logger.info(f"生成文案 (尝试 {attempt + 1}/{settings.max_retries}, 风格: {style}, 档位: {tier})")
            
            # 组装完整 system prompt = 档位段（硬约束前置，结构/字数/时长/金句句界）
            # + 风格段（prompt_optimizer 风格语气模板）
            style_prompt = prompt_optimizer.get_creator_prompt(style)
            creator_prompt = f"{tier_script_guidelines(tier)}\n\n【语气风格】\n{style_prompt}"
            
            # 生成文案
            script_text = await critic_service.generate_script(
                poem_title=poem.title,
                poem_content=poem.content,
                author=poem.author,
                dynasty=poem.dynasty,
                custom_prompt=creator_prompt,
                keywords=keywords,
            )
            
            # 评分
            score_result = await critic_service.score_script(script_text)
            logger.info(f"文案评分: {score_result.score}/10 - {'通过' if score_result.passed else '未通过'}")
            
            # 保存文案（评分 0-10 浮点数）
            script = Script(
                task_id=task.id,
                full_script=script_text,
                score=int(score_result.score),
                score_feedback=score_result.feedback,
                retry_count=attempt,
            )
            db.add(script)
            
            # 更新任务文案和评分
            task.script = script_text
            task.script_score = score_result.score

            # 自动选音色（LLM 推荐为主 + 标题兜底）；失败留空，
            # TTS 阶段回退 config.default_voice_preset → 全局参考音频。
            try:
                from app.services.voice_selector import recommend as _recommend_voice
                task.voice_preset = await _recommend_voice(poem)
                logger.info("自动选音色: poem=%s -> preset=%r",
                            getattr(poem, "title", ""), task.voice_preset)
            except Exception as _ve:
                logger.warning("音色自动选择失败，留空: %s", _ve)
                task.voice_preset = ""

            await db.commit()
            
            if score_result.passed:
                return script_text, score_result
        
        # 所有重试都失败
        return script_text, score_result
    
    async def _generate_storyboard(self, script: str, tier: str = "S") -> list[dict]:
        """生成分镜（每镜含 narration 纯中文旁白）。

        video-comm 文案改造轮 W4：tier 由任务平台解析传入 critic 分镜 prompt
        （镜头预算/镜长窗/narration 上限/五拍弧线/出镜率按档注入）。
        """
        
        storyboard_json = await critic_service.generate_storyboard(script, tier=tier)
        
        try:
            if "```json" in storyboard_json:
                storyboard_json = storyboard_json.split("```json")[1].split("```")[0]
            elif "```" in storyboard_json:
                storyboard_json = storyboard_json.split("```")[1].split("```")[0]
            
            data = json.loads(storyboard_json.strip())
            # 兜底：确保每镜有 narration（逐镜以图定音的核心字段）
            for it in data:
                if not it.get("narration"):
                    it["narration"] = it.get("description", "")
            return data
        except json.JSONDecodeError:
            # 返回档位化默认分镜（S 7 镜 ≈ 6–9 中值 / L 18 镜 ≈ 14–22 中值，镜长按档）
            prof = tier_profile(tier)
            n = max(1, (prof["shots_min"] + prof["shots_max"]) // 2)
            span = (prof["dur_min"] + prof["dur_max"]) / 2.0
            step = span / n
            defaults = []
            t0 = 0.0
            for i in range(n):
                defaults.append({
                    "time": f"{int(t0)}-{int(t0 + step)}s",
                    "description": f"场景 {i + 1}",
                    "camera": "缓慢推进",
                    "has_character": (i % 2 == 0),
                    "narration": f"这是本片的第{i + 1}个画面。",
                })
                t0 += step
            return defaults
    
    async def _generate_images(
        self,
        task: Task,
        storyboard: list[dict],
        db: AsyncSession,
        character_ref: str | None = None,
        style: str = "人生感悟",
        character_description: str | None = None,
        max_parallel: int | None = None,
    ) -> list[str]:
        """生成图片 + 打分循环

        Args:
            task: 任务对象
            storyboard: 分镜列表
            db: 数据库会话
            character_ref: 角色定妆照参考图（i2i 一致性）
            style: 文案风格（用于图片提示词前缀）
            character_description: 角色外貌描述（注入提示词增强一致性）
            max_parallel: 单任务内并发生图数；队列模式传 1（全局并发由队列信号量控制）
        """
        image_urls = []

        async def generate_single(index: int, item: dict):
            # 使用风格化的图片提示词前缀
            image_prefix = prompt_optimizer.get_image_prefix(style)
            # 注入角色描述，增强同一人物一致性（参考 LocalMiniDrama / ArcReel）
            char_clause = f"{character_description}。" if character_description else ""
            # 修 #20260902 "字幕被截断"：送生图前过一遍 _sanitize_image_description，
            # 剥掉 "黑底白字/手写体金句弹出" 这类"要求图内出现文字"的子句，避免：
            # ① 生图模型硬画中文→出乱码/伪字；② 多画幅裁切时文字被截成半截。
            # 同时给句尾补"主体居中、四边留白、安全区"的兜底约束。
            raw_desc = (item.get("description") or "").strip() or "古诗词意境画面"
            safe_desc = self._sanitize_image_description(raw_desc)
            safe_desc += "主体居中，画面四边各留 ≥8% 安全空白，禁止任何文字、金句、字幕、书法字出现在画面内"
            prompt = f"{image_prefix}{char_clause}{safe_desc}，电影级构图，高质量，保持人物一致"

            last_url = None
            last_score = 0.0

            # 图片阶段对 agnes 图片 API 的瞬时 500 较敏感：用指数退避 + 至少 5 次重试，
            # 给外部服务从故障窗口恢复的时间（1s→2s→4s→8s→8s，约 23s 容错窗口）。
            img_retries = max(settings.max_retries, 5)
            for attempt in range(img_retries):
                try:
                    # 角色一致性：把定妆照作为 i2i 参考图回灌（agnes-image-2.1-flash
                    # 支持 extra_body.image，见 agnes-media-generator SKILL.md 图生图章节）。
                    # 若参考图注入异常（极少数情况 agnes 拒收），自动退化为纯文生图，
                    # 避免整批分镜图全部失败（"没有可用的图片"）。
                    try:
                        url = await agnes_client.generate_image(prompt, reference_image=character_ref)
                    except Exception as ref_err:
                        if character_ref:
                            logger.warning(
                                f"图片 {index + 1} i2i 参考注入失败，退化为纯文生图: {ref_err}"
                            )
                            url = await agnes_client.generate_image(prompt)
                        else:
                            raise
                    last_url = url
                    last_url = url

                    # 打分
                    score_result = await critic_service.score_image(url, prompt)
                    last_score = score_result.score
                    logger.info(f"图片 {index + 1} 评分: {score_result.score}/10")

                    if score_result.passed:
                        image_urls.append((index, url))
                        logger.info(f"生成图片 {index + 1} 通过")
                        return
                    else:
                        logger.warning(f"图片 {index + 1} 未通过 ({score_result.score}/10)，重试")

                except Exception as e:
                    logger.warning(
                        f"生成图片 {index + 1} 失败 (尝试 {attempt + 1}/{img_retries}): {e}"
                    )
                # 指数退避，避免对 500 窗口密集重试触发更严厉的限流
                await asyncio.sleep(min(2 ** attempt, 8))

            # 所有重试均未达阈值：使用最后一次生成结果作为兜底，
            # 保证 N 个分镜 shot 产出 N 张图（用户可在审核阶段决定是否重生成单张）
            if last_url:
                logger.warning(
                    f"图片 {index + 1} 评分未达阈值 (最佳 {last_score}/10 < {settings.image_score_threshold})，"
                    f"使用最后一次生成结果作为兜底"
                )
                image_urls.append((index, last_url))
        
        # 受控并发生成图片（尊重 image_concurrency，避免触发 agnes 并发限流 429）
        sem = asyncio.Semaphore(settings.image_concurrency)
        
        async def generate_single_limited(index: int, item: dict):
            async with sem:
                await generate_single(index, item)
        
        # 生成全部分镜图（不再硬编码 [:6]）；上限 30 张，防止 LLM 返回过多分镜时失控
        _shots = storyboard[: min(len(storyboard), 30)]
        coros = [generate_single_limited(i, item) for i, item in enumerate(_shots)]
        await asyncio.gather(*coros)
        
        # 按顺序排列
        image_urls.sort(key=lambda x: x[0])
        return [url for _, url in image_urls]
    
    async def _generate_video(
        self,
        task: Task,
        image_urls: list[str],
        platform_cfg: dict,
        style: str = "人生感悟",
    ) -> str:
        """生成视频（串行队列，使用平台尺寸配置）

        Args:
            task: 任务对象
            image_urls: 图片 URL 列表
            platform_cfg: 平台尺寸配置
            style: 文案风格（用于视频提示词前缀）
        """
        async with self._video_semaphore:
            if not image_urls:
                raise ValueError("没有可用的图片")
            
            # 使用风格化的视频提示词前缀
            video_prefix = prompt_optimizer.get_video_prefix(style)
            prompt = f"{video_prefix}缓慢优雅的镜头运动，电影质感"
            
            video_id = await agnes_client.generate_video(
                prompt=prompt,
                image_url=image_urls[0],
                width=ASPECT_TO_WH.get(platform_cfg["aspect_ratio"], (1152, 768))[0],
                height=ASPECT_TO_WH.get(platform_cfg["aspect_ratio"], (1152, 768))[1],
                num_frames=121,
                frame_rate=24,
            )
            
            logger.info(f"视频任务已提交: {video_id} (平台: {platform_cfg['label']}, 尺寸: {platform_cfg['aspect_ratio']})")
            
            # 轮询等待完成
            result = await agnes_client.poll_video(video_id)

            # agnes 视频结果 URL 字段位置不稳定，兼容多种返回结构
            video_url = _extract_video_url(result)
            if not video_url:
                logger.warning(
                    f"视频轮询返回但未能解析出 URL，原始结果顶层键: {list(result.keys()) if isinstance(result, dict) else type(result)}"
                )
            logger.info(f"视频生成完成: {video_url}")
            
            return video_url
    
    async def _probe_duration(self, path) -> float | None:
        """ffprobe 取音频/视频时长（秒），失败返回 None。"""
        try:
            ffprobe = settings.ffmpeg_path.replace("ffmpeg.exe", "ffprobe.exe")
            p = await asyncio.to_thread(
                subprocess.run,
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, timeout=30,
            )
            if p.returncode == 0 and p.stdout.strip():
                return float(p.stdout.strip())
        except Exception as exc:
            logger.warning("probe 时长失败 %s: %s", path, exc)
        return None

    async def _generate_tts_segments(
        self, task: Task, storyboard: list[dict], db: AsyncSession, force: bool = False
    ) -> dict:
        """逐镜 TTS：对每镜 narration 单独生成旁白，产出 narration_{i}.mp3 + tts_segments.json。

        以图定音：每段旁白独立成文件，时长由 TTS 真实决定（ffprobe），
        后续 _build_segments 用 -shortest 让画面严格贴合旁白，时长天然精准，
        不再出现整段 TTS 倒推幻灯片导致的截断/错位。

        Returns: {"success": bool, "segments":[{index,text,duration,path}],
                  "audio_url": 合并音频URL, "total_duration": float}
        """
        import re as _re
        output_dir = Path(settings.output_dir) / f"task_{task.id}"
        output_dir.mkdir(parents=True, exist_ok=True)
        segs = []
        for i, item in enumerate(storyboard):
            text = (item.get("narration") or item.get("description") or "").strip()
            text = _re.sub(r"\*+", "", text)
            text = _re.sub(r"【[^】]*】", "", text).strip()
            # 安全网：去除拉丁字母单词（KPI / corner / Agnes 等英文品牌词），
            # 避免英文被 TTS 读出（历史上"声声慢"英文旁白根因）。
            text = _re.sub(r"[A-Za-z]+", "", text).strip()
            if text:
                segs.append({"index": i, "text": text})
        if not segs:
            return {"success": False, "error": "无旁白文本"}
        # 复用已有分段（重跑安全）。复用校验四条全过才可复用：
        # 条数一致 / 逐条文本一致（文案改了不得复用旧旁白）/ 文件存在 /
        # 非静音兜底（fallback 标记或 -80dB 以下数字静音，防历史兜底文件毒化成片）。
        seg_json = output_dir / "tts_segments.json"
        if not force and seg_json.exists():
            try:
                existing = json.loads(seg_json.read_text(encoding="utf-8"))
                if len(existing) == len(segs):
                    stale = [
                        s.get("index") for s in existing
                        if s.get("fallback")
                        or not Path(s.get("path", "")).exists()
                        or (s.get("text") or "") != next(
                            (g["text"] for g in segs if g["index"] == s.get("index")), None
                        )
                        or self._is_silent_audio(s.get("path", ""))
                    ]
                    if stale:
                        logger.warning(
                            "task%s TTS 分段含静音/兜底/过期片段 %s，全部重新生成",
                            task.id, stale,
                        )
                    else:
                        logger.info(f"task{task.id} TTS 分段已存在，复用")
                        return await self._finalize_tts(task, existing, output_dir)
            except Exception as exc:
                logger.warning("task%s 复用 TTS 分镜校验失败: %s", task.id, exc)
        if not await tts_client.health_check():
            return {"success": False, "error": "TTS 服务未启动"}
        # edge-tts 单条耗时 15~57s 且偶发 "No audio was received"，并发过高易失败。
        # 上限 2 路并发 + 重试 + 静音兜底，保证每镜都有片段、序号不偏移。
        sem = asyncio.Semaphore(min(int(settings.tts_concurrency), 2))
        results: dict[int, dict] = {}
        # CosyVoice→edge-tts 音色降级警告收集（gen_one 内并发追加）
        degrade_warns: list[str] = []

        # 自动适配音色：task 上 LLM/标题选出的 preset 优先；否则 config.default_voice_preset；
        # 都没有则 None（tts_core 走全局参考音频）。
        chosen_voice_preset = getattr(task, "voice_preset", "") or getattr(settings, "default_voice_preset", "") or None

        # #20260903 杂音根因修复：edge-tts raw 噪声底 -45.5dB 已优于 afftdn 假设
        # (-30dB)，强加降噪反而抬噪/引入处理痕迹；仅对 cosyvoice 等确有底噪/DC
        # 偏移的音源启用 afftdn+acompressor 全链。settings.tts_engine 可能为 auto
        # （运行时才解析），故以 tts_core 返回的实际 r["engine"] 逐段判定（见下方）。

        async def gen_one(idx: int, text: str):
            async with sem:
                # 默认 mp3；cosyvoice 实际产出 wav，必须用对应后缀，
                # 否则「wav 内容塞进 .mp3 文件」会让 ffmpeg concat(-c copy) 失败。
                local = output_dir / f"narration_{idx}.mp3"
                ok = False
                last_err = None
                for attempt in range(1, 4):
                    try:
                        r = await tts_client.generate(
                            text=text, voice=settings.tts_voice,
                            speed=settings.tts_speed, engine=settings.tts_engine,
                            preset_id=chosen_voice_preset,
                        )
                        if r.get("success"):
                            # engine 可能配置为 auto，这里取 tts_core 实际解析结果
                            # (cosyvoice→wav/edge-tts→mp3) 决定 DSP 强度（见 #20260903）。
                            eff_engine = (r.get("engine") or "edge-tts").lower()
                            use_dsp_here = eff_engine not in ("edge-tts", "edge")
                            # CosyVoice 不可用降级 edge-tts 时上浮警告（音色非 preset 原声）
                            if r.get("warning"):
                                degrade_warns.append(str(r["warning"]))
                            path = r.get("audio_path")
                            if path and Path(path).exists():
                                # 合并后 TTS 同进程，直接读本地产物，省去 HTTP 回环
                                suffix = Path(path).suffix or ".mp3"
                                local = output_dir / f"narration_{idx}{suffix}"
                                local.write_bytes(Path(path).read_bytes())
                            else:
                                # 兜底：经 URL 拉取（兼容远程/独立部署场景）
                                url = r.get("audio_url")
                                if url and url.startswith("/"):
                                    url = f"{tts_client.base_url}{url}"
                                async with httpx.AsyncClient(timeout=120.0) as client:
                                    resp = await client.get(url)
                                    resp.raise_for_status()
                                    suffix = Path(url.split("?")[0]).suffix or ".mp3"
                                    local = output_dir / f"narration_{idx}{suffix}"
                                    local.write_bytes(resp.content)
                            dur = await self._probe_duration(local) or (len(text) / 4.0)
                            # 音频清洗：edge-tts raw 噪声底 -45.5dB 本就干净，仅 loudnorm
                            # 统一响度（use_dsp_here=False）；cosyvoice 等确有底噪/DC 偏移
                            # 的音源才走 afftdn+acompressor 全链。失败自动回退原文件。
                            try:
                                cleaned = output_dir / f"narration_{idx}_clean.wav"
                                if await asyncio.to_thread(
                                    self._clean_audio, local, cleaned, 24000, use_dsp_here
                                ):
                                    local = cleaned
                            except Exception as _e:
                                logger.debug(f"镜{idx}音频清洗跳过（异常）: {_e}")
                            results[idx] = {"index": idx, "text": text,
                                            "engine": eff_engine,
                                            "duration": round(dur, 2), "path": str(local)}
                            logger.info(f"task{task.id} TTS 镜 {idx + 1}/{len(segs)} 完成 ({dur:.1f}s, {eff_engine})")
                            ok = True
                            break
                        else:
                            last_err = r.get("error")
                    except Exception as e:
                        last_err = e
                    logger.warning(f"TTS 镜 {idx} 第{attempt}次失败: {last_err}，重试")
                    await asyncio.sleep(2 * attempt)
                if not ok:
                    # 兜底：生成静音片段，确保该镜画面/字幕不丢失、序号不偏移。
                    # 打 fallback 标记：下次复用校验会拒绝并重新生成（2026-09-09
                    # 事故——静音兜底文件被无限复用，成片全程无声）。
                    logger.error(
                        f"TTS 镜 {idx} 最终失败，生成静音兜底片段（已标记 fallback，"
                        f"下次重新生成将自动重试本镜）: {last_err}"
                    )
                    est = max(1.5, len(text) / 4.0)
                    try:
                        sil = await asyncio.to_thread(
                            subprocess.run,
                            [settings.ffmpeg_path, "-y", "-f", "lavfi",
                             "-i", f"anullsrc=r=24000:cl=mono:d={est:.2f}",
                             "-c:a", "libmp3lame", str(local)],
                            capture_output=True, text=True, timeout=60,
                        )
                        if sil.returncode == 0 and local.exists():
                            results[idx] = {"index": idx, "text": text,
                                            "fallback": True,
                                            "error": str(last_err),
                                            "duration": round(est, 2), "path": str(local)}
                    except Exception as e2:
                        logger.error(f"镜 {idx} 静音兜底也失败: {e2}")
                # 兜底：对"主路径清洗未成功"的片段（path 仍是原始 mp3/wav，即非
                # *_clean.wav）再补一次 DSP，让底噪/响度保持一致，避免静音/语音拼接
                # 出现"一句噪声 + 静音 + 一句噪声"的违和节奏。
                # 注意：主路径成功清洗时 cur["path"] 已是 *_clean.wav（与 cleaned 同路径），
                # 必须跳过，否则 src==dst 会让 ffmpeg 用输出截断正在读取的输入文件。
                try:
                    cur = results.get(idx)
                    if cur:
                        cur_path = Path(cur["path"])
                        cleaned = output_dir / f"narration_{idx}_clean.wav"
                        # 静音兜底段无 engine 键 → 按 edge(轻清洗) 处理，避免对静音
                        # 施加强降噪；cosyvoice 段(engine=cosyvoice)保留全链一致性。
                        use_dsp_here = (cur.get("engine") or "edge-tts").lower() not in ("edge-tts", "edge")
                        if cur_path != cleaned and await asyncio.to_thread(
                            self._clean_audio, cur_path, cleaned, 24000, use_dsp_here
                        ):
                            cur["path"] = str(cleaned)
                except Exception as _e:
                    logger.debug(f"镜{idx}兜底音频清洗跳过: {_e}")

        await asyncio.gather(*[gen_one(s["index"], s["text"]) for s in segs])
        ordered = [results[i] for i in sorted(results.keys())]
        if not ordered:
            return {"success": False, "error": "TTS 全部失败"}

        # fail-loud 门禁（详见 _tts_loudness_gate）
        gate = self._tts_loudness_gate(task, ordered)
        if gate:
            return gate

        # 音色降级警告（CosyVoice 不可用 → edge-tts）上浮到 task，
        # 与静音兜底警告共用 error_message 排障痕迹通道。
        if degrade_warns:
            dw = (
                f"TTS 音色降级：{len(degrade_warns)}/{len(ordered)} 段因 CosyVoice2 "
                f"不可用改用 edge-tts 合成（非 preset 原声）。{degrade_warns[0]}"
            )
            logger.warning("task%s %s", task.id, dw)
            task.error_message = dw

        seg_json.write_text(json.dumps(ordered, ensure_ascii=False), encoding="utf-8")
        return await self._finalize_tts(task, ordered, output_dir)

    @staticmethod
    def _tts_loudness_gate(task, ordered: list[dict]) -> Optional[dict]:
        """fail-loud 门禁（2026-09-10 任务005事故，第二次无声片）。

        旧逻辑所有段失败也照常合成静音 audio.mp3 → video 出"成功的无声片"，
        用户侧零提示。现在：
        - 全部段落都是静音兜底 → 返回失败 result（run_stage 的 tts 分支会
          raise → Job 重试 → failed），不再产出无声成片；网络恢复后重新生成即可。
        - 部分失败 → 保留兜底继续（画面/字幕不丢），但在 task.error_message
          留下显式警告（仅 status=failed 时前端渲染红条，待审核态只作排障痕迹）。

        Returns:
            None = 通过（继续合成流程）；dict = 应直接返回的失败 result。
        """
        fallback_segs = [s for s in ordered if s.get("fallback")]
        if not fallback_segs:
            return None
        err_brief = "; ".join(
            f"镜{s['index']}: {(s.get('error') or '未知')[:120]}"
            for s in sorted(fallback_segs, key=lambda x: x["index"])
        )
        if len(fallback_segs) == len(ordered):
            return {
                "success": False,
                "error": (
                    f"TTS 全部 {len(ordered)} 段失败并降级为静音，拒绝出无声片。"
                    f"常见原因：edge-tts 网络不通/代理变更，恢复后重新生成即可。"
                    f"分段错误: {err_brief}"
                ),
            }
        warn = (
            f"TTS 警告：{len(fallback_segs)}/{len(ordered)} 段降级为静音"
            f"（段 {sorted(s['index'] for s in fallback_segs)}），成片对应段落无声。"
            f"失败明细: {err_brief}"
        )
        logger.warning("task%s %s", task.id, warn)
        task.error_message = warn
        return None

    async def _finalize_tts(self, task: Task, segments: list[dict], output_dir: Path) -> dict:
        """合并所有 narration_{i}.* 为 audio.mp3（兼容前端 audio_url），返回总时长。

        注意：分段可能混用 .wav（CosyVoice2 零样本克隆，24000Hz）与 .mp3
        （edge-tts / 静音兜底）。concat demuxer 在 -c copy 下要求所有输入
        容器/编码完全一致，wav+mp3 混合会直接失败。因此这里统一重编码为
        libmp3lame，既能混流又保证输出恒为 audio.mp3（前端按 .mp3 加载）。
        """
        total = sum(s.get("duration", 0) for s in segments)
        narration_files = [s["path"] for s in segments if Path(s["path"]).exists()]
        audio_url = task.audio_url
        if narration_files:
            try:
                listf = output_dir / "narration_list.txt"
                listf.write_text(
                    "\n".join(f"file '{p.replace(chr(92), '/')}'" for p in narration_files),
                    encoding="utf-8",
                )
                audio_out = output_dir / "audio.mp3"
                # 重编码（libmp3lame）以兼容 wav+mp3 混合输入；-c copy 在此会失败。
                res = await asyncio.to_thread(
                    subprocess.run,
                    [settings.ffmpeg_path, "-y", "-f", "concat", "-safe", "0",
                     "-i", str(listf), "-c:a", "libmp3lame", "-ar", "44100",
                     "-b:a", "128k", str(audio_out)],
                    capture_output=True, text=True, timeout=120,
                )
                if res.returncode == 0 and audio_out.exists():
                    audio_url = f"{settings.server_public_url}/outputs/task_{task.id}/audio.mp3"
                    task.audio_url = audio_url
                else:
                    logger.warning(
                        "合并旁白音频失败(rc=%s): %s",
                        res.returncode, (res.stderr or "")[-500:],
                    )
            except Exception as e:
                logger.warning(f"合并旁白音频失败: {e}")
        return {"success": True, "segments": segments, "audio_url": audio_url,
                "total_duration": round(total, 2)}
    
    async def _download_storyboard_images(self, task: "Task", output_dir: Path) -> list:
        """下载 task.image_urls 分镜图到本地，返回成功下载的本地路径列表（失败跳过）。"""
        raw = getattr(task, "image_urls", None)
        try:
            urls = json.loads(raw) if raw else []
        except Exception:
            urls = []
        if not isinstance(urls, list):
            urls = []
        local = []
        for i, u in enumerate(urls):
            if not u or not str(u).startswith("http"):
                continue
            try:
                async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                    resp = await client.get(u)
                    resp.raise_for_status()
                    ctype = (resp.headers.get("content-type") or "").lower()
                    ext = "jpg"
                    if "png" in ctype:
                        ext = "png"
                    elif "webp" in ctype:
                        ext = "webp"
                    p = output_dir / f"img_{i}.{ext}"
                    p.write_bytes(resp.content)
                    if p.stat().st_size > 0:
                        # 文字水印：视频与分镜图统一标识（防搬运/品牌）
                        if settings.watermark_enabled and settings.watermark_text:
                            wm = output_dir / f"img_{i}_wm.png"
                            if await self._apply_image_watermark(settings.ffmpeg_path, p, wm):
                                local.append(str(wm))
                                continue
                        local.append(str(p))
            except Exception as e:
                logger.warning(f"分镜图下载失败 [{u}]: {e}")
        logger.info(f"分镜图可用 {len(local)}/{len(urls)}")
        return local

    async def _probe_image_height(self, path) -> int | None:
        """ffprobe 取图片高度（用于按分辨率推算水印字号）。"""
        try:
            ffprobe = settings.ffmpeg_path.replace("ffmpeg.exe", "ffprobe.exe")
            p = await asyncio.to_thread(
                subprocess.run,
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=height",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, timeout=30,
            )
            if p.returncode == 0 and p.stdout.strip():
                return int(p.stdout.strip())
        except Exception as exc:
            logger.warning("probe 图片高度失败 %s: %s", src, exc)
        return None

    async def _apply_image_watermark(self, ff: str, src: Path, dst: Path) -> bool:
        """给单张分镜图叠加半透明文字水印（drawtext）。失败返回 False，调用方回退原图。"""
        try:
            h = await self._probe_image_height(src) or 1024
            fs = max(18, int(h * settings.watermark_fontsize_ratio))
            margin = max(10, int(h * settings.watermark_margin_ratio))
            xy = (f"x={margin}:y=h-text_h-{margin}" if settings.watermark_position == "left_bottom"
                  else f"x=w-text_w-{margin}:y=h-text_h-{margin}")
            # 本机 drawtext(libfreetype) 走 Fontconfig 且默认配置缺失，字体名方式会
            # 报 "Fontconfig error" 失败；必须用 fontfile 绝对路径（冒号转义为 '\:'）。
            # 颜色用 white@alpha 实现半透明，避免单独的 alpha 选项兼容问题。
            ffont = settings.watermark_fontfile.replace(":", "\\:")
            # 颜色取自设置（watermark_colour），支持 white/black 等；透明度取 watermark_alpha
            wm_colour = (settings.watermark_colour or "white").strip() or "white"
            vf = (f"drawtext=fontfile='{ffont}':"
                  f"text='{settings.watermark_text}':"
                  f"fontcolor={wm_colour}@{settings.watermark_alpha}:"
                  f"fontsize={fs}:{xy}")
            cmd = [ff, "-y", "-i", str(src).replace(chr(92), "/"),
                   "-vf", vf, "-q:v", "2", str(dst).replace(chr(92), "/")]
            res = await asyncio.to_thread(subprocess.run, cmd, capture_output=True,
                                          text=True, timeout=120)
            return res.returncode == 0 and dst.exists() and dst.stat().st_size > 0
        except Exception as e:
            logger.warning(f"图片水印失败: {e}")
            return False

    async def _build_segments(
        self, task: Task, storyboard: list[dict], tts_segments: dict,
        W: int, H: int, platform: str = "douyin"
    ) -> dict:
        """逐镜以图定音：每镜 {img_i + narration_i.mp3} 合成 seg_{platform}_i.mp4（-shortest 严格贴合旁白时长，
        轻微 Ken Burns 推镜），再 concat 为 base_{platform}.mp4。总时长 = Σ旁白时长，天然精准，无 xfade 截断。
        seg/base 加平台前缀确保多平台独立分辨率不串用。

        Returns: {"ok": bool, "base": str, "timeline":[(start,end,text)], "duration": float}
        """
        try:
            ff = settings.ffmpeg_path
            output_dir = Path(settings.output_dir) / f"task_{task.id}"
            output_dir.mkdir(parents=True, exist_ok=True)
            # 确保本地分镜图存在（按 img_{index}.png 命名，对应镜序）
            await self._download_storyboard_images(task, output_dir)
            segs = tts_segments.get("segments", [])
            pairs = []
            for s in segs:
                # 优先用已叠加水印的分镜图（_wm.png），否则退回原图
                wm = output_dir / f"img_{s['index']}_wm.png"
                img = wm if wm.exists() else output_dir / f"img_{s['index']}.png"
                aud = Path(s["path"])
                if img.exists() and aud.exists():
                    pairs.append((s, str(img), str(aud)))
            if not pairs:
                return {"ok": False}
            seg_files = []
            durs = []
            texts = []
            for s, img, aud in pairs:
                # 用旁白 mp3 真实时长（seg 由 -shortest 贴合该时长）做时间轴，
                # 避免用 tts_segments 估值导致结尾截断/多出静帧。
                _adur = await self._probe_duration(str(aud))
                dur = _adur or (s.get("duration") or 3.0)
                dur = max(float(dur), 1.0)
                seg = output_dir / f"seg_{platform}_{s['index']}.mp4"
                # 内存安全的 Ken Burns：把图放大到 1.12x，再用 crop 的 time 表达式
                # 做缓慢平移（sin/cos 漂移）。不依赖 zoompan —— zoompan 在 d=1 时只输出
                # 1 帧导致视频流提前 EOF，在长时长/低内存机器上又会缓冲全部帧而 OOM。
                kb = (f"scale={int(W * 1.12)}:{int(H * 1.12)}:"
                      f"force_original_aspect_ratio=increase,"
                      f"crop={W}:{H}:"
                      f"x='(iw-{W})/2*(1+0.45*sin(2*PI*t/{dur:.2f}))':"
                      f"y='(ih-{H})/2*(1+0.45*cos(2*PI*t/{dur:.2f}))',"
                      f"setsar=1,format=yuv420p")
                cmd = [ff, "-y", "-loop", "1", "-i", img, "-i", aud, "-shortest",
                       "-vf", kb,
                       "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
                       "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", str(seg)]
                res = await asyncio.to_thread(subprocess.run, cmd, capture_output=True,
                                              text=True, timeout=300)
                if res.returncode != 0:
                    # 兜底：纯静态放大（无平移），保证可复现
                    cmd2 = [ff, "-y", "-loop", "1", "-i", img, "-i", aud, "-shortest",
                            "-vf", (f"scale={int(W * 1.12)}:{int(H * 1.12)}:"
                                    f"force_original_aspect_ratio=increase,"
                                    f"crop={W}:{H},setsar=1,format=yuv420p"),
                            "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
                            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", str(seg)]
                    res = await asyncio.to_thread(subprocess.run, cmd2, capture_output=True,
                                                  text=True, timeout=300)
                if res.returncode == 0 and seg.exists() and seg.stat().st_size > 0:
                    # 用 seg 真实时长（而非 mp3 估值）做时间轴与 xfade offset，
                    # 避免 -shortest 量化后的片段时长与 mp3 估值偏差在长链累积漂移
                    _sd = await self._probe_duration(str(seg))
                    if _sd and _sd > 0.1:
                        dur = _sd
                    seg_files.append(str(seg))
                    durs.append(dur)
                    texts.append(s.get("text", ""))
                else:
                    logger.error(f"seg {s['index']} 合成失败: {res.stderr[-300:]}")
            if not seg_files:
                return {"ok": False}

            # 帧量化：seg 按 25fps 编码，时长落在 0.04s 网格；xfade 链式 offset
            # 若用精确浮点会随段数累积漂移（21 段可达 ~0.8s）使末尾越界。统一
            # 量化到帧边界，使 offset 与 ffmpeg 实际帧数一致，消除长链越界。
            FPS = 25
            durs = [max(0.04, round(d * FPS) / FPS) for d in durs]
            # 字幕时间轴 + 总时长：是否含镜间转场决定偏移
            trans = float(settings.transition_duration) if settings.transition_enabled else 0.0
            base = output_dir / f"base_{platform}.mp4"
            n = len(seg_files)
            if trans > 0 and n > 1:
                # 约束：过渡时长不得超过最短片段的 40%，否则 xfade offset 非法
                trans = min(trans, max(min(durs) * 0.4, 0.1))
                timeline, total = self._transition_timeline(durs, texts, trans)
                ok = await self._concat_xfade(ff, seg_files, durs, trans, base)
                if not ok:
                    logger.warning("xfade 转场失败，回退 concat 硬切")
                    timeline, total = self._plain_timeline(durs, texts)
                    ok = await self._concat_copy(ff, seg_files, base)
            else:
                timeline, total = self._plain_timeline(durs, texts)
                ok = await self._concat_copy(ff, seg_files, base)
            if not ok:
                return {"ok": False}
            logger.info(f"逐镜 seg 合成完成: {n} 段，总时长 {total:.1f}s (trans={trans:.2f})")
            return {"ok": True, "base": str(base), "timeline": timeline, "duration": round(total, 2)}
        except Exception as e:
            logger.error(f"逐镜 seg 合成异常: {e}")
            return {"ok": False}

    # ----- 片段拼接辅助：硬切 / 交叉淡入淡出 -----
    @staticmethod
    def _plain_timeline(durs: list, texts: list) -> tuple:
        """无转场：时间轴 = 片段时长累加。"""
        timeline = []
        t = 0.0
        for d, tx in zip(durs, texts):
            timeline.append((t, t + d, tx))
            t += d
        return timeline, t

    @staticmethod
    def _transition_timeline(durs: list, texts: list, trans: float) -> tuple:
        """有转场：第 k 段起点 = Σ前(k-1)段时长 - k·trans（交叉淡入淡出重叠），
        总时长 = Σ时长 - (n-1)·trans。"""
        timeline = []
        cum = 0.0
        for k, (d, tx) in enumerate(zip(durs, texts)):
            start = cum - k * trans
            timeline.append((start, start + d, tx))
            cum += d
        total = cum - (len(durs) - 1) * trans
        return timeline, total

    async def _concat_copy(self, ff: str, seg_files: list, base: Path) -> bool:
        """硬切拼接（concat demuxer -c copy，最快，但无过渡）。"""
        listf = base.parent / "seg_list.txt"
        listf.write_text("\n".join(f"file '{p.replace(chr(92), '/')}'"
                                   for p in seg_files), encoding="utf-8")
        res = await asyncio.to_thread(
            subprocess.run,
            [ff, "-y", "-f", "concat", "-safe", "0", "-i", str(listf),
             "-c", "copy", str(base)],
            capture_output=True, text=True, timeout=300,
        )
        return res.returncode == 0 and base.exists()

    async def _concat_xfade(self, ff: str, seg_files: list, durs: list, trans: float, base: Path) -> bool:
        """交叉淡入淡出：视频 xfade 消除硬切；音频用 concat 滤镜 gapless 拼接
        （acrossfade 在本机合并滤镜图中会丢失输出标签，已在诊断中确认弃用）。

        durs 须为帧量化（25fps → 0.04s 网格）时长：xfade 链式 offset 用
        acc_end-trans 计算，量化后与实际编码帧数对齐，长链（21 段）不再越界。
        """
        n = len(seg_files)
        if n == 1:
            try:
                import shutil
                shutil.copy(seg_files[0], base)
                return base.exists()
            except Exception:
                return False
        ttype = "fade" if settings.transition_type == "fade" else settings.transition_type
        vparts = []
        prev_v = "[0:v]"
        out_v = "v1"
        acc_end = durs[0]  # 已累加片段的当前时长（随转场递减）
        for k in range(1, n):
            # 偏移 = 前段结束点 - 过渡时长；durs 已帧量化，与 ffmpeg 实际帧数对齐，
            # 不再需要 0.04s 余量（余量反而会在长链引入新的累积误差）。
            off = max(acc_end - trans, 0.05)
            vparts.append(
                f"{prev_v}[{k}:v]xfade=transition={ttype}:"
                f"duration={trans:.2f}:offset={off:.3f}[{out_v}]"
            )
            prev_v = f"[{out_v}]"
            out_v = f"v{k+1}"
            acc_end = acc_end - trans + durs[k]
        # 音频 gapless 拼接（concat 滤镜，稳健且不会在合并图中缺失输出标签）
        ainputs = "".join(f"[{k}:a]" for k in range(n))
        afc = f"{ainputs}concat=n={n}:v=0:a=1[aout]"
        fc = ";".join(vparts) + ";" + afc
        inputs = []
        for f in seg_files:
            inputs += ["-i", str(f).replace(chr(92), "/")]
        # -map 视频用最后一次 xfade 实际产出标签 v{n-1}；音频用 concat 的 [aout]
        cmd = [ff, "-y", *inputs, "-filter_complex", fc,
               "-map", f"[v{n-1}]", "-map", "[aout]",
               "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", str(base)]
        res = await asyncio.to_thread(subprocess.run, cmd, capture_output=True,
                                      text=True, timeout=600)
        if res.returncode != 0:
            logger.error(f"xfade 拼接失败: {res.stderr[-400:]}")
            return False
        return base.exists() and base.stat().st_size > 0

    def _select_bgm(self, style: str):
        """按文案风格选古风 BGM（低音量铺底）。真实 mp3 优先，缺失回退合成 wav。"""
        bgm_dir = Path(__file__).resolve().parents[2] / "assets" / "bgm"
        if not bgm_dir.exists():
            return None
        mood = style or "人生感悟"
        m = "general"
        if any(k in mood for k in ("忧", "悲", "伤", "愁", "凄", "苦", "怨", "哀")):
            m = "sad"
        elif any(k in mood for k in ("思", "乡", "怀", "忆", "念")):
            m = "nostalgic"
        elif any(k in mood for k in ("壮", "豪", "激", "志", "慷", "烈", "昂")):
            m = "epic"
        elif any(k in mood for k in ("静", "淡", "悠", "闲", "雅", "宁", "清")):
            m = "calm"
        cands = sorted(bgm_dir.glob(f"{m}_*.mp3"))
        if not cands and m == "sad":
            cands = sorted(bgm_dir.glob("nostalgic_*.mp3")) or sorted(bgm_dir.glob("calm_*.mp3"))
        if not cands:
            for alt in ("nostalgic", "calm", "general", "epic"):
                cands = sorted(bgm_dir.glob(f"{alt}_*.mp3"))
                if cands:
                    break
        if not cands:
            cands = sorted(bgm_dir.glob("*.wav"))
        if not cands:
            return None
        import random
        return str(cands[random.randrange(len(cands))])

    # ===== 文本/图像/音频后处理工具 =====
    # 提示词工程层已经把"图内禁文字/四边留白"指令注入分镜 prompt；
    # 这里再加一道**保险闸**：分镜 description 进生图前把残留的"图内文字"描述子句
    # 全部剔除，避免 LLM 偶发仍写"金句弹出""黑底白字"。这些字眼若不剥，必然
    # 让生图模型画中文 → 出乱码 → 多画幅裁切时被截成半截字。
    _TEXT_IN_IMAGE_TRIGGERS = re.compile(
        r"(?:"
        r"黑底白字|手写体|手写金句|手写|书法字|书法|印刷体|"
        r"金句弹出|金句浮现|金句定格|金句文字|金句缓缓|"
        r"字幕定格|字幕弹出|字幕浮现|字幕叠加|字幕文字|"
        r"题诗出现|题诗浮现|题字出现|题字浮现|"
        r"水印文字|徽记文字|印章文字|题款文字|"
        r"calligraphy|chinese text|text overlay|caption card|handwritten phrase"
        r")"
    )
    _LONG_BRACKET_QUOTE = re.compile(r"「[^」]{6,}」|《[^》]{6,}》|『[^』]{6,}』")
    _PAREN_CLAUSE = re.compile(r"[\(（][^()（）]{2,60}[\)）]")

    @staticmethod
    def _sanitize_image_description(desc: str) -> str:
        """分镜 description 在送生图前做内容清洗：剥掉"图内文字"要求+括号里的话术标注。

        修复 #20260902 "字幕被截断"：分镜 prompt 已加入"图内禁文字/四边留白"
        强约束，但分镜 LLM 偶发仍会写出"黑底白字，手写体金句弹出"之类句子
        （脚本里的"画面"描述被原样照搬到 description）。这些要求若不剥，
        生图模型会画中文 → 出乱码 → 多画幅切到 9:16/3:4 时被裁切成半截字。
        """
        if not desc:
            return "古诗词意境画面"
        s = str(desc)
        # 1) 去掉括号内的全部备注（导演话术/制作说明都不该进图）
        s = PipelineEngine._PAREN_CLAUSE.sub("", s)
        # 2) 去掉长引号/书名号包裹的金句题词
        s = PipelineEngine._LONG_BRACKET_QUOTE.sub("", s)
        # 3) 去掉残留的图内文字要求关键词
        s = PipelineEngine._TEXT_IN_IMAGE_TRIGGERS.sub("", s)
        # 4) 多余标点/空格归一化
        s = re.sub(r"\s{2,}", " ", s)
        s = re.sub(r",\s*,+", ",", s)
        s = re.sub(r"^[,\s，]+|[,\s，]+$", "", s)
        return s.strip() or "古诗词意境画面"

    @staticmethod
    def _af_chain(noise_reduce: bool, sr: int) -> str:
        """构造 TTS 清洗 ffmpeg 滤镜链。

        #20260903 杂音根因修复：noise_reduce=False 时去掉 afftdn/acompressor。
        背景：afftdn=nf=-30 假设噪声底在 -30dB，而 edge-tts 原始输出噪声底实测仅
        -45.5dB（比假设更干净），滤波器模型失配反而把底噪抬到 -35.4dB、听感发"沙"；
        acompressor makeup 再补一刀。故对 edge-tts 段禁用降噪，cosyvoice 等确有
        DC/底噪(-39dB)的音源保留全链。
        """
        noise_filters = (
            "afftdn=nf=-30:tn=1,"
            "acompressor=threshold=-18dB:ratio=2.5:attack=20:release=250:makeup=2,"
        ) if noise_reduce else ""
        return (
            f"highpass=f=60,"
            f"{noise_filters}"
            f"loudnorm=I=-14:TP=-1:LRA=11,"
            f"aresample={sr}:resampler=soxr"
        )

    @staticmethod
    def _is_silent_audio(path: str | Path) -> bool:
        """检测音频是否为数字静音（max_volume <= -80dB，anullsrc 兜底产物特征）。

        用于 TTS 分段复用校验：历史上静音兜底文件（TTS 3 连失败时 anullsrc 生成）
        因"文件存在"被无限复用，成片全程无声。解码失败/无音轨按非静音处理
        （交由调用方其他校验把关），避免误杀正常片段。
        """
        p = Path(path)
        if not p.exists() or p.stat().st_size == 0:
            return False
        try:
            res = subprocess.run(
                [settings.ffmpeg_path, "-hide_banner", "-i", str(p),
                 "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"],
                capture_output=True, text=True, timeout=30,
            )
            m = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", res.stderr)
            return bool(m) and float(m.group(1)) <= -80.0
        except Exception:
            return False

    @staticmethod
    def _clean_audio(src: Path, dst: Path, sr: int = 24000, noise_reduce: bool = True) -> bool:
        """TTS 旁白音频清洗：去 DC + 可选底噪抑制 + 响度归一。

        行业实践（参考 AWS Polly / ElevenLabs 后期建议）：
        - 高通 f=60 砍掉 DC offset（cosyvoice 实测 DC offset≈-0.000147）和低频嗡嗡声；
        - afftdn（非实时频域降噪 RNNoise）削掉合成器引入的稳态噪声（实测噪声底 ~-39dB）；
        - acompressor 拉平 dynamic range，拗口的旁白读得更稳；
        - loudnorm 把响度统一到 ~-14 LUFS（短视频平台友好区间），峰值 ≤-1 dBTP。
        失败时回退 raw 文件（调用方不阻断），并打印 ffmpeg stderr 以供排障。

        noise_reduce=False 时走轻链（仅 loudnorm），参数细节见 _af_chain 的 #20260903 说明。
        """
        if not Path(src).exists() or Path(src).stat().st_size == 0:
            return False
        try:
            ff = settings.ffmpeg_path
            af_chain = PipelineEngine._af_chain(noise_reduce=noise_reduce, sr=sr)
            cmd = [
                ff, "-y", "-hide_banner", "-loglevel", "warning",
                "-i", str(src),
                "-af", af_chain,
                "-ac", "1", "-ar", str(sr),
                "-c:a", "pcm_s16le", str(dst),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if res.returncode == 0 and Path(dst).exists() and Path(dst).stat().st_size > 0:
                return True
            logger.warning("TTS 音频清洗失败，保留原文件: %s", (res.stderr or "")[-200:])
        except Exception as e:
            logger.warning("TTS 音频清洗异常，保留原文件: %s", e)
        return False

    @staticmethod
    def _fmt_srt(sec: float) -> str:
        h = int(sec // 3600); m = int((sec % 3600) // 60); s = sec % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")

    @staticmethod
    def _aspect_key(W: int, H: int) -> str:
        """把渲染分辨率映射为画幅键，用于查 chars_per_line 等按画幅配置。"""
        if not H:
            return "9:16"
        r = W / H
        if abs(r - 9 / 16) < 0.06:
            return "9:16"
        if abs(r - 3 / 4) < 0.06:
            return "3:4"
        return "16:9"

    @staticmethod
    def _split_cue(text: str, max_chars: int = 15) -> list[str]:
        """把长旁白句按标点拆成 ≤max_chars 的短句(标点附后)；单 clause 超长则硬切。"""
        import re
        segs = re.split(r"([，。！？；：、])", text)
        parts, buf = [], ""
        for s in segs:
            if s in "，。！？；：、":
                buf += s
                parts.append(buf)
                buf = ""
            else:
                buf += s
        if buf:
            parts.append(buf)
        parts = [p for p in parts if p.strip()]
        chunks, cur = [], ""
        for p in parts:
            if len(cur) + len(p) <= max_chars:
                cur += p
            else:
                if cur:
                    chunks.append(cur)
                if len(p) > max_chars:
                    while len(p) > max_chars:
                        chunks.append(p[:max_chars])
                        p = p[max_chars:]
                    cur = p
                else:
                    cur = p
        if cur:
            chunks.append(cur)
        return chunks or [text]

    @staticmethod
    def _wrap_lines(text: str, chars_per_line: int) -> str:
        """按字宽硬换行：每 ≤chars_per_line 字一行，行间用 \\N 连接。

        中文无空格，ASS 不会自动折行，故必须显式插入 \\N 硬换行符，
        保证长文案按指定字数折行、不溢出画面、不被裁切（\\N 硬换行不受 WrapStyle 影响）。
        """
        text = str(text).replace("\r", "").replace("\n", "").replace("\\N", "")
        if not text:
            return text
        step = max(1, int(chars_per_line))
        lines = [text[i:i + step] for i in range(0, len(text), step)]
        return "\\N".join(lines)

    def _split_timeline(self, timeline: list, chars_per_line: int = 12, max_lines: int = 2,
                        golden_lines: list | None = None,
                        golden_weight: float | None = None) -> list:
        """把每个长 cue 拆成更短字幕 cue(每条 ≤ chars_per_line*max_lines 字)，再每条内部
        按 chars_per_line 硬换行(\\N)，每条 1~max_lines 行，按字数比例分配时间(每屏≥0.8s, 间隔0.15s)。

        目的：① 诗词旁白单句长(40~70字)，按标点拆成多条时序字幕(每条≤ chars_per_line*max_lines 字)，
        每条屏显 1~2 行，字号可跑满基准(竖屏≈86px)，小屏清晰可读；② 每条内部再按 chars_per_line
        硬换行（ASS 反斜杠N），双保险彻底根治「长句不换行→溢出画面被裁切」问题。

        video-comm 决策 5（金句字幕加权停留）：golden_lines 传入金句候选集时，命中金句的
        cue 按 golden_weight 倍率放大"字数当量"再参与同镜时长池分配——停留时间显著长于普通
        过渡句，金句是可截图的"最小传播单元"，停留不足等于浪费。命中判定见 _is_golden_cue。
        """
        gweight = float(golden_weight) if golden_weight else 0.0
        max_chars = max(chars_per_line, chars_per_line * max(1, int(max_lines)))
        out = []
        for (st, en, text) in timeline:
            chunks = self._split_cue(text, max_chars)
            if len(chunks) == 1:
                out.append((st, en, self._wrap_lines(chunks[0], chars_per_line)))
                continue
            dur = max(en - st, 0.1)
            # 加权当量：金句 cue 按 golden_weight 放大参与时长分配（golden_weight<=1 视为关闭）
            wlens = [len(c) * (gweight if (gweight > 1.0 and self._is_golden_cue(c, golden_lines))
                               else 1.0) for c in chunks]
            total_w = sum(wlens) or 1
            gap = 0.15
            t = st
            for c, wlen in zip(chunks, wlens):
                cd = max(0.8, dur * wlen / total_w)
                ce = min(en, t + cd)
                out.append((t, ce, self._wrap_lines(c, chars_per_line)))
                t = min(en, ce + gap)
                if t >= en:
                    break
        return out

    @staticmethod
    def _is_golden_cue(text: str, golden_lines: list | None = None) -> bool:
        """金句字幕判定（video-comm 决策 5，首批启发式 v1）：

        ① golden_lines 候选集子串命中（原诗整句/引号内句子，len>=4 防噪音）；
        ② cue 自带引号包裹子句（「…」/『…』/“…”/‘…’）——作者有意引用/强调的句子。
        快档三段式文案落地（critic prompt 强制原诗金句整句出现）后，① 将成为主信号。
        """
        if not text:
            return False
        if golden_lines:
            for g in golden_lines:
                g = (g or "").strip()
                if len(g) >= 4 and g in text:
                    return True
        import re as _re
        return bool(_re.search(r"[「『“‘][^」』”’]{2,40}[」』”’]", text))

    @staticmethod
    def _duration_tier_warning(total: float, tier: str | None = None,
                               platform: str | None = None) -> str | None:
        """成片时长 vs 档位窗口校验（video-comm 文案改造轮 W5）：超窗/超硬上限仅告警，不自动截断。

        截断会剪掉收尾定格与互动留白，属内容层问题——由文案/评审修复后重跑更安全。
        优先级：硬上限 > 低于下限 > 高于目标窗。窗口来自 config.TIER_PROFILES（单一事实源）。
        """
        prof = tier_profile(tier)
        lo, hi = prof["dur_min"], prof["dur_max"]
        hard = prof.get("dur_hard_max")
        who = f"（平台 {platform}）" if platform else ""
        if hard is not None and total > hard:
            return (f"成片 {total:.1f}s 超过 {prof['label']}档硬上限 {hard}s{who}"
                    f"——超长伤完播，建议人工复核文案/分镜后重跑")
        if total < lo:
            return f"成片 {total:.1f}s 低于 {prof['label']}档目标下限 {lo}s{who}——过短可能信息不足"
        if total > hi:
            return f"成片 {total:.1f}s 超出 {prof['label']}档目标窗 {lo}–{hi}s{who}——偏长需留意完播"
        return None

    @staticmethod
    def _split_poem_full_lines(content: str) -> list[str]:
        """把原诗全文切成"整句"候选（供金句池原诗整句匹配，video-comm 决策 5 升级）。

        按换行分句 → 每行一律按标点（，。；！？、等）细切成子句 → 去首尾标点/引号。
        只保留 4–40 字片段（<4 防噪音；>40 过长无法被子幕 cue 整句命中，丢弃）。
        子句粒度入池 + _is_golden_cue 的子串命中 → 无论旁白是念单句
        （"床前明月光"）还是整联（"举头望明月，低头思故乡"）都能命中。
        例："床前明月光，疑是地上霜。\n举头望明月，低头思故乡。"
        → [床前明月光, 疑是地上霜, 举头望明月, 低头思故乡]
        """
        if not content:
            return []
        import re as _re
        out: list[str] = []
        for raw in _re.split(r"[\n\r]+", content):
            line = raw.strip()
            if not line:
                continue
            for seg in _re.split(r"[，。；！？、,.!?;:]", line):
                seg = seg.strip().strip("… \t“”‘’\"'「」『』（）()·")
                if 4 <= len(seg) <= 40 and seg not in out:
                    out.append(seg)
        return out

    @staticmethod
    def _extract_golden_from_script(script: str, poem_content: str | None = None) -> list:
        """金句候选池抽取（video-comm 决策 5）：

        ① 文案脚本(task.script)中「引号内句子」——S 三段式 prompt 强制金句以原诗整句
          形态 + 「」标注出现（critic 档位段【金句句界】），此处抽出即得池主体；
        ② poem_content 传入时并入【原诗整句】池——白话段旁白常原样念出原诗整句
          （不带引号），字幕 cue 仅凭"自带引号"启发会漏掉 → 原诗池使其同样获加权停留。
        无引号/无脚本/无原诗时返回空表，字幕层退化为"cue 自带引号"启发（旧行为）。
        """
        out: list[str] = []
        if script:
            import re as _re
            for g in _re.findall(r"[「『“‘]([^」』”’]{2,40})[」』”’]", script):
                g = g.strip()
                if len(g) >= 4 and g not in out:
                    out.append(g)
        if poem_content:
            for line in PipelineEngine._split_poem_full_lines(poem_content):
                if line not in out:
                    out.append(line)
        return out

    def _write_segmented_srt(self, timeline, path: Path):
        lines = []
        for i, (st, en, text) in enumerate(timeline, 1):
            lines.append(str(i))
            lines.append(f"{self._fmt_srt(st)} --> {self._fmt_srt(en)}")
            lines.append(text)
            lines.append("")
        Path(path).write_text("\n".join(lines), encoding="utf-8")

    @staticmethod
    def _adaptive_subtitle_fontsize(
        timeline: list, W: int, H: int, base_fs: int,
        margin_h_ratio: float, max_block_ratio: float, min_fs: int,
    ) -> int:
        """按最长单句反推字号，约束字幕块高度 ≤ max_block_ratio*H。

        中文全角字形宽 ≈ 字号(px)。可用宽度 = W*(1-2*margin_h_ratio)；
        chars_per_line = floor(avail_w / fs)；lines = ceil(max_chars / chars_per_line)；
        块高 ≈ lines * fs * 1.15(行距)。超出上限则等比重缩字号，下限 min_fs。
        """
        max_chars = 0
        for (_s, _e, text) in timeline:
            # 文本内已含 \N 硬换行（每条时序字幕 1~max_lines 行），需按 \N 拆行统计最长行。
            for ln in str(text).replace("\\N", "\n").split("\n"):
                ln = ln.strip()
                if ln:
                    max_chars = max(max_chars, len(ln))
        if max_chars == 0:
            return base_fs
        avail_w = W * (1.0 - 2.0 * margin_h_ratio)
        if avail_w <= 0:
            return base_fs
        chars_per_line = max(1, int(avail_w / base_fs))
        lines = (max_chars + chars_per_line - 1) // chars_per_line
        line_spacing = 1.15
        block_h = lines * base_fs * line_spacing
        max_block_h = H * max_block_ratio
        if block_h > max_block_h:
            fs = int(base_fs * (max_block_h / block_h))
            return max(min_fs, fs)
        return base_fs

    async def _burn_subtitles(
        self, task: Task, base_path: str, storyboard: list[dict], segs: dict,
        W: int = 1080, H: int = 1350,
        platform: str = "douyin",
        poem_content: str | None = None,
    ) -> str:
        """烧录分段字幕 + 混入场景 BGM，产出 final_{platform}.mp4（多平台）或 final.mp4。

        视频基底 = 逐镜 seg 合成的 base_{platform}.mp4（已含旁白音轨）。
        彻底弃用 agnes source.mp4 音轨：画面与旁白均来自逐镜以图定音的结果；
        BGM 按 style 选古风曲低音量铺底。字幕预设/字号随平台分辨率(W,H)缩放。
        """
        try:
            output_dir = Path(settings.output_dir) / f"task_{task.id}"
            base = Path(base_path)
            if not base.exists() or base.stat().st_size == 0:
                logger.error("base.mp4 不存在，无法烧录")
                return task.video_url or ""
            # 多平台模式：非默认平台用 final_{platform}.mp4 避免覆盖
            suffix = f"_{platform}" if platform != "douyin" else ""
            final_path = output_dir / f"final{suffix}.mp4"
            srt_path = output_dir / "subtitle.srt"
            timeline = segs.get("timeline") or []
            if not timeline:
                t = 0.0
                for s in segs.get("segments", []):
                    d = s.get("duration", 3.0)
                    timeline.append((t, t + d, s.get("text", "")))
                    t += d
            # 长旁白句(诗词单句40~70字)拆成多条短 cue(每条≤ chars_per_line*max_lines 字)，
            # 每条内部再按 chars_per_line 硬换行(\\N)：每屏 1~2 行，字数变少→
            # 自适应字号可跑满基准(竖屏≈86px)，小屏清晰可读；拆分后 timeline 同时
            # 用于写 SRT 与下方自适应字号计算，根治「长句不换行→溢出画面被裁切」问题。
            ar = self._aspect_key(W, H)
            cpl = int((getattr(settings, 'subtitle_chars_per_line', None) or {}).get(ar, 12))
            max_lines = int(getattr(settings, 'subtitle_max_lines', 2)) or 2
            # video-comm 决策 5：金句候选集 = 文案脚本(task.script)中引号包裹句子 +
            # 原诗整句（poem_content 传入时并入——白话段旁白不带引号念出原诗也命中）；
            # 命中金句的 cue 在 _split_timeline 按 golden_subtitle_weight 放大停留。
            golden_lines = self._extract_golden_from_script(
                getattr(task, "script", "") or "", poem_content)
            gweight = float(getattr(settings, "golden_subtitle_weight", 1.4) or 0.0)
            # video-comm 决策 5：末镜静音定格秒数（0=关闭，命令与旧版完全一致）
            hold = float(getattr(settings, "end_hold_duration", 2.0) or 0.0)
            timeline = self._split_timeline(timeline, chars_per_line=cpl, max_lines=max_lines,
                                            golden_lines=golden_lines, golden_weight=gweight)
            # 权威时长 = _build_segments 累加的 Σ(旁白时长 - 转场重叠)，
            # 不依赖 probe_duration(base)：xfade 重编码后元数据虽准，但硬切路径
            # concat -c copy 仍可能失真（如报 197s 实为 159s），统一以 segs.duration 为准。
            total = segs.get("duration") or self._probe_duration(base) or 30.0
            # 末镜定格（video-comm 决策 5）：SRT 末条字幕结束时间延至定格窗末尾——
            # 收尾句在画面定格期间保持屏显，给完播与截图传播留窗口（画面定格/音频静音
            # 由下方 ffmpeg tpad/apad 实现，此处只负责字幕时间轴）。
            if hold > 0 and timeline:
                _last = timeline[-1]
                timeline[-1] = (_last[0], round(total + hold, 3), _last[2])
            self._write_segmented_srt(timeline, srt_path)
            # 字幕预设：古风楷体(推荐, kai) / 安全黑体(yahei) / 旧版(default)
            style = SUBTITLE_STYLES.get(settings.subtitle_style, SUBTITLE_STYLES["kai"])
            # 字号随分辨率缩放（config subtitle_fontsize_ratio，默认0.045→竖屏1920≈86px，
            # 贴近抖音70-95/小红书48-72/B站横屏56px 行业区间；上限120下限56保证可读）。
            base_fs = max(56, min(120, int(H * getattr(settings, 'subtitle_fontsize_ratio', 0.045))))
            # 底部边距（config subtitle_margin_v_ratio，默认0.25→竖屏1920≈480px）；
            # 抖音/小红书底部 18~20% 被 UI 遮挡，0.25 留 5% 净空于 UI 顶缘之上，彻底摆脱遮挡。
            mv = max(60, int(H * getattr(settings, 'subtitle_margin_v_ratio', 0.25)))
            # 左右安全边距（config subtitle_margin_h_ratio，默认6%→1080宽约65px），避免长句贴边
            mh = max(20, int(W * getattr(settings, 'subtitle_margin_h_ratio', 0.06)))
            # 自适应字号：按最长单句反推，约束字幕块高度 ≤ max_block_ratio*H，
            # 防止超长句（无标点/极长文案）仍溢出画面（下限 subtitle_min_fontsize）。
            fs = self._adaptive_subtitle_fontsize(
                timeline, W=W, H=H, base_fs=base_fs,
                margin_h_ratio=getattr(settings, 'subtitle_margin_h_ratio', 0.06),
                max_block_ratio=getattr(settings, 'subtitle_max_block_height_ratio', 0.10),
                min_fs=getattr(settings, 'subtitle_min_fontsize', 28),
            )
            # WrapStyle：默认0=智能均衡换行（双保险，配合 SRT 内 \N 硬换行彻底杜绝长句裁切）。
            wrap_style = int(getattr(settings, 'subtitle_wrap_style', 0))
            # 描边加粗（≥4）+ 阴影（≥2）+ 加粗(Bold)：研究建议粗体无衬线确保小屏可读；楷体亦加 Bold 合成粗体
            outline = max(style.get('outline', 2), 4)
            shadow = max(style.get('shadow', 1), 2)
            force_style = (f"FontName={style['font']},FontSize={fs},Bold=1,"
                           f"PrimaryColour={style['primary_colour']},"
                           f"OutlineColour={style['outline_colour']},"
                           f"Outline={outline},Shadow={shadow},"
                           f"Alignment={style['alignment']},MarginV={mv},"
                           f"MarginL={mh},MarginR={mh},WrapStyle={wrap_style},"
                           f"PlayResX={W},PlayResY={H}")
            def _esc(p):
                return str(p).replace("\\", "/").replace(":", "\\:")
            srt_esc = _esc(srt_path)
            base_fwd = str(base).replace("\\", "/")
            bgm = self._select_bgm(task.style)
            out_total = total + (hold if hold > 0 else 0.0)
            # video-comm 文案改造轮 W5：成片时长 vs 平台所属档位窗口校验
            # （超窗/超硬上限仅告警日志，不自动截断——截断会剪掉收尾定格与互动留白）。
            _warn = self._duration_tier_warning(out_total, tier_of(platform), platform)
            if _warn:
                logger.warning(f"task{task.id} 时长校验({platform}, {out_total:.1f}s): {_warn}")
            if bgm:
                bgm_fwd = str(bgm).replace("\\", "/")
                if hold > 0:
                    # 末镜定格：视频 tpad 克隆末帧 hold 秒；音频 apad 补静音（BGM 续铺），
                    # amix duration=first 取旁白原长，随后 apad 使总长 = total + hold。
                    fc = (
                        f"[0:v]subtitles='{srt_esc}':force_style='{force_style}'[v1];"
                        f"[v1]tpad=stop_mode=clone:stop_duration={hold:.2f}[v];"
                        f"[0:a]volume=1.0[a0];[1:a]volume=0.22[a1];"
                        f"[a0][a1]amix=inputs=2:duration=first[aout];"
                        f"[aout]apad=pad_dur={hold:.2f}[afin]"
                    )
                    cmd = [
                        settings.ffmpeg_path, "-y",
                        "-i", base_fwd,
                        "-stream_loop", "-1", "-i", bgm_fwd,
                        "-filter_complex", fc,
                        "-map", "[v]", "-map", "[afin]",
                        "-t", f"{out_total:.2f}",
                        "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                        str(final_path),
                    ]
                else:
                    cmd = [
                        settings.ffmpeg_path, "-y",
                        "-i", base_fwd,
                        "-stream_loop", "-1", "-i", bgm_fwd,
                        "-filter_complex",
                        (f"[0:v]subtitles='{srt_esc}':force_style='{force_style}'[v];"
                         f"[0:a]volume=1.0[a0];[1:a]volume=0.22[a1];"
                         f"[a0][a1]amix=inputs=2:duration=first[aout]"),
                        "-map", "[v]", "-map", "[aout]",
                        "-t", f"{out_total:.2f}",
                        "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                        str(final_path),
                    ]
            else:
                if hold > 0:
                    # 无 BGM：统一走 filter_complex（视频定格 + 音频补静音），保持单命令入口
                    fc = (
                        f"[0:v]subtitles='{srt_esc}':force_style='{force_style}'[v1];"
                        f"[v1]tpad=stop_mode=clone:stop_duration={hold:.2f}[v];"
                        f"[0:a]apad=pad_dur={hold:.2f}[afin]"
                    )
                    cmd = [
                        settings.ffmpeg_path, "-y",
                        "-i", base_fwd,
                        "-filter_complex", fc,
                        "-map", "[v]", "-map", "[afin]",
                        "-t", f"{out_total:.2f}",
                        "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                        str(final_path),
                    ]
                else:
                    cmd = [
                        settings.ffmpeg_path, "-y",
                        "-i", base_fwd,
                        "-vf", f"subtitles='{srt_esc}':force_style='{force_style}'",
                        "-map", "0:v:0", "-map", "0:a:0",
                        "-t", f"{out_total:.2f}",
                        "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                        str(final_path),
                    ]
            logger.info(f"FFmpeg 烧录字幕+BGM: task_{task.id} (bgm={bool(bgm)}, dur={out_total:.1f}s, hold={hold:.1f}s)")
            result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True,
                                              text=True, timeout=600)
            if result.returncode != 0 or not final_path.exists() or final_path.stat().st_size == 0:
                logger.error(f"FFmpeg 失败: {result.stderr[-500:]}")
                return task.video_url or ""
            final_url = f"{settings.server_public_url}/outputs/task_{task.id}/final{suffix}.mp4"
            task.subtitle_url = final_url
            try:
                task.video_duration = round(out_total, 1)
            except Exception as exc:
                logger.warning("计算成片时长失败: %s", exc)
            logger.info(f"字幕烧录完成: {final_url} (时长 {out_total:.1f}s, bgm={bool(bgm)})")
            return final_url
        except Exception as e:
            logger.error(f"字幕烧录异常: {e}")
            return task.video_url or ""
    
    def _extract_clean_text(self, script_text: str) -> str:
        """从脚本中提取「配音/旁白」正文，供 TTS 与字幕使用。

        兼容脚本生成器的多种结构：
        - 结构A：每段以 **文案：** 或 文案： 标记，旁白在冒号后同一行；
        - 结构B：以 **旁白（音色描述）：** 或 旁白（音色描述）： 标记，
          正文为紧随其后的 `>` 引用行（可能多行）。
        注意：实际产出中 旁白 标记**未必加粗**（如 task1 用「旁白（冷峻、直接）：」），
        旧正则要求 `**旁白...：**` 会整段漏匹配、退化为把整篇脚本当旁白，
        导致 TTS 读出「画面：...」制作备注与 `**【标题】**` 残留的星号。
        现改为逐行抽取：只保留以 旁白/文案 开头（取冒号后正文）或 `>` 引用行，
        其余（标题、**【...】**、（画面：...）、（黑屏...）、视频标题、制作建议等）
        一律丢弃。全局去除 markdown `*` 与 `【】`。
        """
        import re
        if not script_text:
            return ""
        # 1) 全局去除 markdown 加粗/星号（**...** 或孤立 *），避免星号被 TTS 读出
        text = re.sub(r'\*{1,2}', '', script_text)
        narration = []
        for ln in text.splitlines():
            s = ln.strip()
            if not s:
                continue
            # 结构B：以 > 引用行承载旁白
            if s.startswith(">"):
                s = re.sub(r'^>\s*', '', s).strip()
                if s:
                    narration.append(s)
                continue
            # 旁白 标记行：旁白（音色描述）：正文 或 旁白：正文
            m = re.match(r'^旁白(?:\s*[（(][^）)]*[）)])?\s*[：:]\s*(.*)$', s)
            if m:
                body = m.group(1).strip()
                if body:
                    narration.append(body)
                continue
            # 文案 标记行：文案：正文
            m2 = re.match(r'^文案(?:\s*[（(][^）)]*[）)])?\s*[：:]\s*(.*)$', s)
            if m2:
                body = m2.group(1).strip()
                if body:
                    narration.append(body)
                continue
            # 其余（标题、（画面：...）、（黑屏...）、视频标题、制作建议等）丢弃
        out = "\n".join(narration)
        out = re.sub(r'【[^】]*】', '', out)   # 保险：旁白内若残留【】也去掉
        out = re.sub(r'\s+', ' ', out).strip()
        return out


# 全局引擎实例
pipeline_engine = PipelineEngine()
