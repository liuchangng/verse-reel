"""任务管理 API"""
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Depends, Query, BackgroundTasks, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.database import get_db, async_session_factory
from app.models.task import Task
from app.models.job import Job
from app.services.queue import STAGE_ORDER, queue_service
from app.models.poem import Poem
from app.services.pipeline import pipeline_engine
from app.services.prompt_optimizer import list_styles, normalize_style, DEFAULT_STYLE
from app.services.publisher import publisher_service
from app.services.task_state import patch_task

logger = logging.getLogger(__name__)

router = APIRouter()


def _cleanup_task_output(task_id: int) -> None:
    """删除任务产物目录 output/task_{id}（安全加固 REQ-S4.5）。

    删除 DB 记录后调用，避免孤儿产物占盘；清理失败仅告警不阻塞接口。
    """
    from app.config import settings
    out_dir = Path(getattr(settings, "output_dir", "") or "server/data/output")
    task_dir = out_dir / f"task_{task_id}"
    try:
        if task_dir.is_dir():
            shutil.rmtree(task_dir)
            logger.info("已清理任务 %s 产物目录 %s", task_id, task_dir)
    except Exception as exc:
        logger.warning("清理任务 %s 产物目录失败 %s: %s", task_id, task_dir, exc)


class BatchGenerateRequest(BaseModel):
    """批量生成请求体（#30 批量入队）"""
    task_ids: Optional[list[int]] = None   # 指定任务（优先于 status）
    status: Optional[str] = None           # 按状态筛选：failed/all/pending_review...
    stage: str = "all"                      # all | script|character|image|tts|video|subtitle
    clear_outputs: bool = True              # 是否清空旧产物（默认全量重跑）


@router.get("/")
async def list_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None, description="状态筛选"),
    db: AsyncSession = Depends(get_db),
):
    """获取任务列表"""
    query = select(Task)
    count_query = select(func.count(Task.id))
    
    if status:
        query = query.where(Task.status == status)
        count_query = count_query.where(Task.status == status)
    
    total = await db.execute(count_query)
    total = total.scalar()
    
    offset = (page - 1) * page_size
    query = query.offset(offset).limit(page_size).order_by(Task.id.desc())
    result = await db.execute(query)
    tasks = result.scalars().all()

    # 批量获取关联诗词（消除 N+1：一次 IN 查询建 map，替代循环内逐条 db.get）
    poem_ids = {t.poem_id for t in tasks if t.poem_id}
    poems_map: dict[int, Poem] = {}
    if poem_ids:
        pres = await db.execute(select(Poem).where(Poem.id.in_(poem_ids)))
        poems_map = {p.id: p for p in pres.scalars().all()}

    # 获取关联的诗词信息
    items = []
    for task in tasks:
        poem = poems_map.get(task.poem_id)
        # 基于实际数据推算状态
        _img_urls = []
        if task.image_urls:
            try: _img_urls = json.loads(task.image_urls)
            except Exception as exc:
                logger.warning("任务 %s image_urls 解析失败: %s", task.id, exc)
        _script_done = bool(task.script and task.script_score)
        _image_done = bool(_img_urls) or (task.image_score is not None and task.image_score > 0)
        _video_done = bool(task.video_url)
        items.append({
            "id": task.id,
            "poem_id": task.poem_id,
            "poem_title": poem.title if poem else "未知",
            "poem_author": poem.author if poem else "未知",
            "status": task.status,
            "current_stage": task.current_stage,
            "progress": task.progress,
            "platform": task.platform,
            "script_status": "done" if _script_done else ("processing" if task.current_stage == "script" else "pending"),
            "image_status": "done" if _image_done else ("processing" if task.current_stage == "image" else "pending"),
            "video_status": "done" if _video_done else ("processing" if task.current_stage == "video" else "pending"),
            "script_score": task.script_score,
            "image_score": task.image_score,
            "character_ref": task.character_ref,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        })
    
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/styles")
async def list_task_styles():
    """可选文案风格列表（创建任务弹窗下拉用）。

    返回 [{name, description}]；前端传空/不传 style = 「自动推荐」，
    由后端按热点主题推断，无热点时回退默认风格。
    """
    return {"items": list_styles(), "default": DEFAULT_STYLE}


@router.post("/")
async def create_task(
    poem_id: int,
    platform: str = Query("douyin", description="主平台(用于主视频分辨率/主URL)"),
    platforms: list[str] = Query(None, description="本任务选中的发布平台列表(多选)；为空则回退全局 settings.output_platforms"),
    source_hotspot_title: Optional[str] = None,
    source_keywords: Optional[list[str]] = None,
    style: Optional[str] = Query(None, description="文案风格(见 GET /api/tasks/styles)；不传=自动推荐"),
    db: AsyncSession = Depends(get_db),
):
    """创建新任务并自动启动流水线"""
    # 检查诗词是否存在
    poem = await db.get(Poem, poem_id)
    if not poem:
        raise HTTPException(status_code=404, detail="诗词不存在")

    # 主平台取列表首个(或默认 douyin)，列表本身透传给 create_task
    if platforms:
        platform = platforms[0]
    else:
        platforms = None

    # 未知风格直接 400，避免静默回退到默认风格（用户以为选中了实际没生效）
    if style and normalize_style(style) is None:
        valid = [s["name"] for s in list_styles()]
        raise HTTPException(status_code=400, detail=f"未知风格: {style}；可选: {valid}")

    # 创建任务（热点来源随任务落库：文案阶段只注入本任务自己的热点）
    task = await pipeline_engine.create_task(
        db, poem_id, platform, platforms,
        source_hotspot_title=source_hotspot_title,
        source_keywords=source_keywords,
        style=style,
    )

    # 统一生命周期（2026-09-09）：创建即入队。旧版这里用 BackgroundTasks 直跑
    # run_pipeline，完全绕过队列——无执行记录（进度弹窗"暂无执行记录"）、
    # 不受任务间串行约束、后端重启即死且无从恢复（僵尸 processing）。
    # 与 regenerate/batch 同一架构：全部走 generation_jobs 队列。
    count = await queue_service.enqueue_task(task_id=task.id, source="create_task")

    return {
        "id": task.id,
        "status": "pending",
        "enqueued_count": count,
        "message": "任务创建成功，已加入生成队列（可在任务详情查看各阶段执行记录）",
    }


@router.delete("/batch-clear")
async def clear_all_tasks(
    confirm: str = Query("NO", description="二次确认：必须传 confirm=YES 才执行（安全加固 REQ-S3）"),
    db: AsyncSession = Depends(get_db),
):
    """清空全部任务数据（级联清理 jobs + scripts + 产物文件）

    ⚠️ 破坏性操作：删除 tasks 全行、关联 generation_jobs、scripts 表，
    以及 server/data/output/ 下所有 task_* 产物目录。用于「重新开始」场景。
    安全加固：必须 confirm=YES + 破坏性限频，否则 400/429。
    """
    from app.services.rate_limiter import api_limiter_destructive

    # 二次确认（在删除逻辑之前拦截，杜绝无副作用误触发）
    if confirm != "YES":
        raise HTTPException(status_code=400, detail="危险操作：必须传 confirm=YES 确认")
    # 破坏性操作限频（满窗立即 429）
    if not await api_limiter_destructive.acquire(timeout=0):
        raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")

    from sqlalchemy import text

    # 1. 统计即将清除的数据
    task_count = (await db.execute(select(func.count(Task.id)))).scalar() or 0
    job_count = (await db.execute(select(func.count(text("*"))).select_from(text("generation_jobs")))).scalar() or 0
    script_count = (await db.execute(select(func.count(text("*"))).select_from(text("scripts")))).scalar() or 0

    # 2. 收集所有任务 output 目录路径（删 DB 前先收集）
    output_base = None
    try:
        from app.config import settings
        output_base = getattr(settings, 'output_dir', None) or 'server/data/output'
    except ImportError:
        output_base = 'server/data/output'
    if not os.path.isabs(output_base):
        output_base = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', output_base))

    task_dirs = []
    if os.path.isdir(output_base):
        for d in os.listdir(output_base):
            full = os.path.join(output_base, d)
            if os.path.isdir(full) and d.startswith('task_'):
                task_dirs.append(full)

    # 3. 删除数据库记录（顺序：jobs → scripts → tasks，避免 FK 约束）
    await db.execute(text("DELETE FROM generation_jobs"))
    await db.execute(text("DELETE FROM scripts"))
    await db.execute(text("DELETE FROM tasks"))
    await db.commit()

    # 4. 删除产物目录
    removed_dirs = 0
    for d in task_dirs:
        try:
            shutil.rmtree(d)
            removed_dirs += 1
        except Exception as e:
            logger.warning(f"清理产物目录失败 {d}: {e}")

    logger.info(
        f"批量清空完成: tasks={task_count} jobs={job_count} scripts={script_count} "
        f"output_dirs={removed_dirs}/{len(task_dirs)}"
    )
    return {
        "message": "已清空全部任务数据",
        "deleted": {
            "tasks": task_count,
            "generation_jobs": job_count,
            "scripts": script_count,
            "output_directories": removed_dirs,
        },
    }


@router.get("/{task_id}")
async def get_task(task_id: int, db: AsyncSession = Depends(get_db)):
    """获取单个任务详情"""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    poem = await db.get(Poem, task.poem_id)
    
    # 解析 storyboard JSON 字符串为列表
    storyboard = None
    if task.storyboard:
        try:
            storyboard = json.loads(task.storyboard)
        except (json.JSONDecodeError, TypeError):
            storyboard = []
    else:
        storyboard = []

    # 解析 image_urls JSON
    image_urls_list = None
    if task.image_urls:
        try:
            image_urls_list = json.loads(task.image_urls)
        except (json.JSONDecodeError, TypeError):
            image_urls_list = []

    # 状态推算：基于实际数据存在性，不依赖 current_stage（避免结束后 stage 回退导致误判）
    _script_done = bool(task.script and task.script_score)
    _image_done = bool(image_urls_list) or (task.image_score is not None and task.image_score > 0)
    _video_done = bool(task.video_url)
    _audio_done = bool(task.audio_url)
    _subtitle_done = bool(task.subtitle_url)

    return {
        "id": task.id,
        "poem_id": task.poem_id,
        "poem_title": poem.title if poem else "未知",
        "poem_author": poem.author if poem else "未知",
        "dynasty": poem.dynasty if poem else "未知",
        "content": poem.content if poem else "",
        "status": task.status,
        "current_stage": task.current_stage,
        "progress": task.progress,
        "platform": task.platform,
        "script": task.script,
        "script_status": "done" if _script_done else ("processing" if task.current_stage == "script" else "pending"),
        "image_status": "done" if _image_done else ("processing" if task.current_stage == "image" else "pending"),
        "video_status": "done" if _video_done else ("processing" if task.current_stage == "video" else "pending"),
        "script_score": task.script_score,
        "image_score": task.image_score,
        "image_urls": image_urls_list,
        "character_ref": task.character_ref,
        "character_status": "done" if task.character_ref else ("processing" if task.current_stage == "character" else "pending"),
        "audio_url": task.audio_url,
        "audio_status": "done" if _audio_done else ("processing" if task.current_stage == "tts" else "pending"),
        "subtitle_url": task.subtitle_url,
        "subtitle_status": "done" if _subtitle_done else ("processing" if task.current_stage == "subtitle" else "pending"),
        "video_url": task.video_url,
        "video_duration": task.video_duration,
        # 多平台成片 URL 映射（JSON 字符串）：前端视频预览按平台数量渲染多个成片。
        # 缺省/单平台时为空，前端回退到单个 video_url。
        "platform_outputs": task.platform_outputs,
        "storyboard": storyboard,
        "error_message": task.error_message,
        # 审核相关（Q2A）
        "review_status": task.review_status,
        "review_comment": task.review_comment,
        "reviewed_at": task.reviewed_at.isoformat() if task.reviewed_at else None,
        "reviewable": task.status == "pending_review",
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }


@router.get("/{task_id}/jobs")
async def get_task_jobs(task_id: int, db: AsyncSession = Depends(get_db)):
    """任务各阶段执行记录（任务列表"进度/日志"弹窗数据源）。

    返回按标准阶段顺序排列的 Job 列表：状态 / 尝试次数 / 每次尝试历史
    （attempts_log，max_retries=3 → 最多 3 条失败 + 1 条成功）/ 起止时间 / 错误。
    """
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    res = await db.execute(
        select(Job).where(Job.task_id == task_id).order_by(Job.id.asc())
    )
    all_jobs = res.scalars().all()  # id 升序 = 时间正序；只取一次，后面复用
    # 同一阶段可能有多条 Job（重新生成保留终态旧 Job 作历史）：按阶段聚合。
    # 尝试日志按 Job.id 顺序拼接并连续编号（跨轮不重号），**输出最新在前**
    # （2026-09-09 用户定夺：日志最新的在前面，旧的在后面）；阶段卡片按
    # 最近活动时间倒序——刚跑完/正在跑的阶段排最上面；状态/错误/起止取最新。
    by_stage: dict[str, list[Job]] = {}
    for j in all_jobs:
        by_stage.setdefault(j.stage, []).append(j)

    def _latest_ts(group: list[Job]):
        j = group[-1]
        return j.finished_at or j.started_at or j.created_at

    out = []
    for stage, group in by_stage.items():
        latest = group[-1]
        merged = []
        seq = 0
        for j in group:  # id 升序 = 时间正序，编号连续累加
            try:
                logs = json.loads(j.attempts_log) if j.attempts_log else []
            except (json.JSONDecodeError, TypeError):
                logs = []
            for a in logs:
                seq += 1
                a["attempt"] = seq
                merged.append(a)
        merged.reverse()  # 最新的在前面
        out.append({
            "stage": stage,
            "status": latest.status,
            "attempts": seq,
            "attempts_log": merged,
            "last_error": latest.last_error,
            "started_at": latest.started_at.isoformat() if latest.started_at else None,
            "finished_at": latest.finished_at.isoformat() if latest.finished_at else None,
            "_ts": _latest_ts(group),
        })
    from datetime import datetime as _dt
    out.sort(key=lambda x: x.pop("_ts") or _dt.min, reverse=True)

    # 扁平执行日志（2026-09-09 用户定夺形态）：编号/阶段/状态/时间一张表，
    # 全任务跨阶段跨轮次全局连续编号，按时间倒序输出（最新在最上面）。
    flat = []
    seq = 0
    for j in all_jobs:
        try:
            logs = json.loads(j.attempts_log) if j.attempts_log else []
        except (json.JSONDecodeError, TypeError):
            logs = []
        for a in logs:
            seq += 1
            a["seq"] = seq
            a["stage"] = j.stage
            flat.append(a)
    flat.reverse()

    return {"task_id": task_id, "task_status": task.status, "jobs": out, "logs": flat}


@router.post("/{task_id}/start")
async def start_task(
    task_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """启动任务（后台执行）"""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    if task.status == "processing":
        raise HTTPException(status_code=400, detail="任务正在执行中")
    
    if task.status == "done":
        raise HTTPException(status_code=400, detail="任务已完成")
    
    # 在后台执行流水线
    async def run_in_background():
        async with async_session_factory() as session:
            try:
                await pipeline_engine.run_pipeline(session, task_id)
            except Exception as e:
                logger.error(f"后台任务失败: {e}")
    background_tasks.add_task(run_in_background)
    
    return {
        "id": task_id,
        "status": "processing",
        "message": "任务已启动",
    }


@router.delete("/{task_id}")
async def delete_task(task_id: int, db: AsyncSession = Depends(get_db)):
    """删除任务（安全加固：删除记录后级联清理产物目录，避免孤儿文件占盘）"""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    if task.status == "processing":
        raise HTTPException(status_code=400, detail="无法删除执行中的任务")
    
    # 级联删除子表记录（2026-09-09 事故修复）：generation_jobs / scripts 无
    # DB 外键级联，漏删会留下孤儿 Job —— 孤儿按优先级永远占据串行队列的
    # "活动任务"位且永远不被消费，把其他任务的 Job 全部饿死。
    from sqlalchemy import delete as sa_delete
    from app.models.script import Script
    await db.execute(sa_delete(Job).where(Job.task_id == task_id))
    await db.execute(sa_delete(Script).where(Script.task_id == task_id))
    await db.delete(task)
    await db.commit()
    # 级联清理产物目录（失败仅告警，不阻断删除成功返回）
    _cleanup_task_output(task_id)
    
    return {"message": "任务已删除"}


@router.post("/{task_id}/publish")
async def publish_task(
    task_id: int,
    platforms: list[str] = Query(["douyin"], description="发布平台列表"),
    confirm: str = Query("NO", description="二次确认：必须传 confirm=YES 才执行（安全加固 REQ-S3）"),
    db: AsyncSession = Depends(get_db),
):
    """发布任务视频到各平台

    安全加固：对外发布属高风险操作，必须 confirm=YES 二次确认，否则 400。
    """
    # 二次确认（在业务查询之前拦截）
    if confirm != "YES":
        raise HTTPException(status_code=400, detail="发布需二次确认：必须传 confirm=YES")

    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    if task.status != "done":
        raise HTTPException(status_code=400, detail="任务未完成，无法发布")
    
    if not task.video_url:
        raise HTTPException(status_code=400, detail="没有可发布的视频")
    
    # 获取诗词信息
    poem = await db.get(Poem, task.poem_id)
    if not poem:
        raise HTTPException(status_code=404, detail="诗词不存在")
    
    # 生成发布内容
    content = await publisher_service.generate_publish_content(
        script=task.script or "",
        poem_title=poem.title,
        platform=task.platform,
    )
    
    # 发布到各平台
    results = await publisher_service.publish_to_multiple(
        video_path=task.video_url,
        title=content["title"],
        description=content["description"],
        tags=content["tags"],
        platforms=platforms,
    )
    
    return {
        "task_id": task_id,
        "results": [
            {"platform": r.platform, "success": r.success, "message": r.message}
            for r in results
        ],
    }


@router.post("/{task_id}/publish-content")
async def generate_task_publish_content(
    task_id: int,
    platforms: list[str] = Query(["douyin", "xiaohongshu", "kuaishou"], description="要生成文案的平台列表"),
    db: AsyncSession = Depends(get_db),
):
    """按需生成各平台的发布文案（标题/描述/话题），供详情页视频卡片下方展示。

    设计（2026-09-10 Q3）：前端"查看时按需生成"——不进流水线、不建 DB 字段，
    用 LLM 按各平台字数/话题规范产出一组吸睛文案，进程内按 (task_id, script 哈希,
    platforms) 缓存，同文案重复进详情页不重复调 LLM。LLM 失败的平台回退规则版。
    """
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 诗词元信息（作者/朝代）增强文案相关性；缺失不阻断（LLM 仍可用标题+脚本）
    poem = await db.get(Poem, task.poem_id)
    copy = await publisher_service.generate_platform_copy(
        task_id=task_id,
        script=task.script or "",
        poem_title=(poem.title if poem else "") or "",
        author=(poem.author if poem else "") or "",
        dynasty=(poem.dynasty if poem else "") or "",
        platforms=platforms,
    )
    return {"task_id": task_id, "content": copy}


@router.post("/{task_id}/review")
async def review_task(
    task_id: int,
    action: str = Query(..., description="approve | reject"),
    comment: Optional[str] = Query(None, description="审核意见"),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: AsyncSession = Depends(get_db),
):
    """人工审核任务（Q2A 发布前审核）

    - approve: 审核通过 → status=done + review_status=approved + reviewed_at
    - reject:  审核驳回 → status=failed + review_status=rejected + review_comment=comment
    - 仅当任务处于 pending_review 时可审核，否则 400
    """
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status != "pending_review":
        raise HTTPException(status_code=400, detail="任务不在待审核状态")

    if action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="action 必须是 approve 或 reject")

    now = datetime.now()
    if action == "approve":
        fields = dict(
            status="done", current_stage="done", progress=100,
            review_status="approved", completed_at=now, reviewed_at=now,
        )
    else:
        fields = dict(
            status="failed", review_status="rejected",
            review_comment=comment or "未填写审核意见", reviewed_at=now,
        )

    # 原子 CAS（2026-09-10 并发加固 P1-D）：上面的 `task.status != pending_review`
    # 校验与这里的写入之间仍存在窗口——双重提交、或队列终态判定同时改写 status，
    # 都会让一方的修改被另一方静默覆盖。改由数据库按 status 条件执行 UPDATE，
    # 条件不满足即说明状态已被他人改变，返回 409 让前端刷新而不是悄悄丢改动。
    if not await patch_task(
        db, task_id, only_if={"status": "pending_review"}, **fields
    ):
        raise HTTPException(
            status_code=409,
            detail="任务状态已变更（可能已被审核或重新生成），请刷新后重试",
        )
    await db.refresh(task)
    logger.info(f"任务 {task_id} 审核: {action}, review_status={task.review_status}")

    return {
        "task_id": task_id,
        "status": task.status,
        "review_status": task.review_status,
        "review_comment": task.review_comment,
        "reviewed_at": task.reviewed_at.isoformat() if task.reviewed_at else None,
    }


@router.post("/{task_id}/regenerate")
async def regenerate_task(
    task_id: int,
    stage: str = Query("script", description="重跑阶段: script/image/video/all"),
    force: bool = Query(False, description="覆盖在途 Job：任务仍有 pending/running 阶段时默认拒绝（409）"),
    db: AsyncSession = Depends(get_db),
):
    """重新生成任务（Q3A）

    改造：旧版用 BackgroundTasks 直接调 ``run_pipeline``，**绕过队列**。
    新版入库：
    - ``stage=all``：入队全部 6 阶段，清空全部旧产物
    - ``stage={script, character, image, tts, video, subtitle}``：入队单阶段，
      只清空**该阶段自己的**产物字段（``STAGE_OUTPUTS``），其余阶段产物保留。
      例：``stage=image`` 只清 ``image_urls/image_score``，保留文案与定妆照。
    - 依赖判定走 ``_task_produced`` 产物自检（D5 死锁修复），无需前置 Job
    - 兼容旧名：``storyboard`` → ``image``，``spot`` → ``script``
    """
    from app.services.queue import queue_service, STAGE_ORDER

    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    stage_alias = {"storyboard": "image", "spot": "script"}
    stage = stage_alias.get(stage, stage)

    # 在途互斥（2026-09-09 事故）：重新入队会删除本任务所有 pending/running Job
    # 并清空对应产物 —— 任务正在跑时"又重新生成一遍"会把已跑完的分镜/配音/视频
    # 悄悄丢掉重来，用户看到的就是"我没点重生成，图片怎么又变了一批"。
    # 规则：默认拒绝（409 并列出在途阶段），前端二次确认后带 force=true 才放行。
    if not force:
        inflight = (await db.execute(
            select(Job).where(Job.task_id == task_id, Job.status.in_(("pending", "running")))
        )).scalars().all()
        if inflight:
            detail = "、".join(f"{j.stage}({j.status})" for j in inflight)
            logger.warning(
                "重新生成被拒：task=%s 仍有在途 Job stage=%s 在途=[%s]",
                task_id, stage, ",".join(f"#{j.id}:{j.stage}:{j.status}" for j in inflight),
            )
            raise HTTPException(
                status_code=409,
                detail=f"任务正在执行中（{detail}），重新生成会丢弃在途进度与已生成产物；"
                       f"确认请带 force=true 重试",
            )

    if stage == "all":
        stages = list(STAGE_ORDER)
        clear_outputs = True
    elif stage in STAGE_ORDER:
        stages = [stage]
        # 单阶段重跑也必须清空该阶段自己的产物，否则 run_stage 的产物自检会直接
        # 跳过，「重生成」变成空操作。清空范围由 STAGE_OUTPUTS 限定为当前阶段
        # 字段（例：image 只清 image_urls/image_score，保留 script / character_ref）。
        clear_outputs = True
    else:
        raise HTTPException(
            status_code=400,
            detail=f"未知阶段: {stage}（合法: script/character/image/tts/video/subtitle/all）",
        )

    # 立即翻 status=processing 前端不用等队列 claim 才看到状态变化。
    # 原子 CAS（2026-09-10 并发加固 P1-D）：以「读到的 status」为条件写入，
    # 期间若被审核/终态判定改写则更新不命中 → 409，避免两个写者互相覆盖。
    if not await patch_task(
        db, task_id,
        only_if={"status": task.status},
        status="processing", progress=0, review_status="pending",
        error_message=None, current_stage=stage,
    ):
        raise HTTPException(
            status_code=409,
            detail="任务状态已被其他操作改变，请刷新后重试",
        )
    await db.refresh(task)

    count = await queue_service.enqueue_task(
        task_id=task_id, stages=stages, clear_outputs=clear_outputs,
        source="regenerate",
    )

    return {
        "task_id": task_id,
        "stage": stage,
        "enqueued_stages": stages,
        "enqueued_count": count,
        "clear_outputs": clear_outputs,
        "status": task.status,
        "message": f"已加入生成队列（stage={stage}），可在 /api/queue/status 查看进度",
    }


@router.post("/batch/generate")
async def batch_generate(
    req: BatchGenerateRequest,
    db: AsyncSession = Depends(get_db),
):
    """批量入队生成（#30）

    按 ``task_ids`` 或 ``status`` 筛选，把多个任务批量加入生成队列。
    跨任务交错并发由队列的「阶段信号量 + 优先级」自然实现：
      - 文本/图片/角色/字幕 跨任务并发（受各类型并发数限制）
      - 视频 跨任务串行（video_concurrency=1 + 62s 最小间隔）
    故接口层无需额外调度，直接逐任务 ``enqueue_task`` 即可。各任务内部的
    依赖补全（``_expand_prereqs``）与死锁自愈（``_heal_prereqs``）照常生效。
    安全加固：批量操作接口限频（满窗 429）。
    """
    from app.services.queue import queue_service, STAGE_ORDER
    from app.services.rate_limiter import api_limiter_heavy

    if not await api_limiter_heavy.acquire(timeout=0):
        raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")

    stage_alias = {"storyboard": "image", "spot": "script"}
    stage = stage_alias.get(req.stage, req.stage)
    if stage != "all" and stage not in STAGE_ORDER:
        raise HTTPException(
            status_code=400,
            detail=f"未知阶段: {stage}（合法: script/character/image/tts/video/subtitle/all）",
        )

    # 1) 解析目标任务
    if req.task_ids:
        res = await db.execute(select(Task).where(Task.id.in_(req.task_ids)))
        tasks = res.scalars().all()
        found = {t.id for t in tasks}
        missing = [i for i in req.task_ids if i not in found]
        if missing:
            logger.warning(f"批量生成：忽略不存在的任务 {missing}")
    elif req.status:
        res = await db.execute(select(Task).where(Task.status == req.status))
        tasks = res.scalars().all()
    else:
        raise HTTPException(status_code=400, detail="必须提供 task_ids 或 status 其中之一")

    if not tasks:
        return {"enqueued_tasks": 0, "total_jobs": 0, "tasks": [],
                "message": "无匹配任务"}

    # 2) 逐任务入队（依赖补全 + 死锁自愈在队列内自动处理）
    results = []
    total_jobs = 0
    stages_arg = list(STAGE_ORDER) if stage == "all" else [stage]
    for t in tasks:
        try:
            # 立即翻 processing + current_stage，前端实时可见（不必等队列 claim）
            t.status = "processing"
            t.progress = 0
            t.review_status = "pending"
            t.error_message = None
            t.current_stage = "script" if stage == "all" else stage
            count = await queue_service.enqueue_task(
                task_id=t.id, stages=stages_arg, clear_outputs=req.clear_outputs,
                source="batch_generate",
            )
            total_jobs += count
            results.append({"task_id": t.id, "enqueued_count": count, "ok": True})
        except Exception as e:
            logger.error(f"批量生成：任务 {t.id} 入队失败: {e}")
            results.append({"task_id": t.id, "enqueued_count": 0, "ok": False, "error": str(e)[:200]})

    await db.commit()
    return {
        "enqueued_tasks": len([r for r in results if r["ok"]]),
        "total_jobs": total_jobs,
        "tasks": results,
        "message": f"已批量加入生成队列（stage={stage}），跨任务交错并发，可在 /api/queue/status 查看进度",
    }
