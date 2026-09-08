"""FastAPI 主应用入口"""
import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query, Header, Depends
from fastapi.exceptions import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from app.config import settings
from app.database import init_db
from app.services.settings_store import (
    load_config_from_db,
    save_config_to_db,
    reset_config_in_db,
    apply_overlay,
    WRITABLE_KEYS,
)
from app.services.queue import queue_service
from app.api import poems, tasks, ws, hotspots, tts
from app.services import tts_core

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


async def require_token(authorization: str | None = Header(default=None)):
    """Bearer Token 鉴权依赖（安全加固 REQ-S2）。

    - app_token 未配置（空）→ 503 fail-closed（服务配置问题，拒绝一切受保护访问）
    - Authorization 头 != "Bearer <app_token>" → 401
    /health 与 / 不挂本依赖（探活豁免）。
    """
    if not settings.app_token:
        raise HTTPException(status_code=503, detail="服务未配置 APP_TOKEN（请在 .env 设置后重启）")
    if authorization != f"Bearer {settings.app_token}":
        raise HTTPException(status_code=401, detail="未授权：token 缺失或不匹配")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时
    logger.info("正在启动古诗词视频流水线...")
    await init_db()
    logger.info("数据库初始化完成")

    # 加载持久化配置（DB overlay），让重启后保留用户在系统设置页的修改。
    # 仅对白名单字段生效，非法值会被忽略。
    overlay = await load_config_from_db()
    if overlay:
        apply_overlay(settings, overlay)
        logger.info("已应用持久化配置：%d 项", len(overlay))

    # 启动生产队列（DB 持久化 + 重启可恢复）。
    # 必须在 settings overlay 之后启动，这样 queue 的并发信号量读到的是用户值。
    await queue_service.start()
    logger.info("生成队列已上线")

    # 预热 CosyVoice2：若配置了参考音频，后台线程加载模型，避免首个 TTS 请求承担
    # ~14s 加载开销。参考音频未配置则跳过（auto 走 edge-tts）。失败不影响启动。
    tts_core.prewarm_cosyvoice()

    yield
    # 关闭时
    logger.info("正在关闭应用...")
    await queue_service.stop()


# 创建 FastAPI 应用
app = FastAPI(
    title="古诗词短视频工厂",
    description="基于 Agnes AI 的古诗词短视频自动化生产系统",
    version="0.1.0",
    lifespan=lifespan,
)

# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由（安全加固：全部 /api 挂 Bearer Token 鉴权，见 require_token）
app.include_router(poems.router, prefix="/api/poems", tags=["诗词"], dependencies=[Depends(require_token)])
app.include_router(tasks.router, prefix="/api/tasks", tags=["任务"], dependencies=[Depends(require_token)])
app.include_router(hotspots.router, prefix="/api/hotspots", tags=["热点"], dependencies=[Depends(require_token)])
# TTS 已合并进主后端（进程内，默认 edge-tts，CosyVoice2 就绪后 auto 优先）
app.include_router(tts.router, prefix="/api/tts", tags=["TTS"], dependencies=[Depends(require_token)])
# WebSocket 不走 HTTP 依赖，token 在 ws.py 端内校验（query_params["token"]）
app.include_router(ws.router, prefix="/ws", tags=["WebSocket"])

# 静态产物服务（review IMPORTANT-1）：原 StaticFiles 裸挂载不继承 router 鉴权依赖，
# 导致 /outputs 下的成片/音频/字幕可无 token 下载。改为显式路由 serve_output：
# - Authorization 头或 ?token= 查询参数均可（浏览器媒体请求只能走后者）
# - abspath 前缀校验防目录穿越（..、%2e%2e 等）
# 例如 /outputs/task_1/final.mp4
import mimetypes

os.makedirs(settings.output_dir, exist_ok=True)


@app.get("/outputs/{path:path}")
async def serve_output(
    path: str,
    authorization: str | None = Header(default=None),
    token: str = "",
):
    """产物文件服务（review IMPORTANT-1：带鉴权 + 目录穿越防护）。

    浏览器 <video>/<img>/window.open 的媒体请求无法携带 Authorization 头，
    故额外接受 ?token= 查询参数（与 WS 同模式）。
    令牌为"公开同源"语义（见 client/.env.example），不作为机密凭证使用。
    """
    import secrets

    if not settings.app_token:
        raise HTTPException(status_code=503, detail="服务未配置 APP_TOKEN（请在 .env 设置后重启）")
    authorized = False
    if authorization:
        authorized = secrets.compare_digest(authorization, f"Bearer {settings.app_token}")
    if not authorized and token:
        authorized = secrets.compare_digest(token, settings.app_token)
    if not authorized:
        raise HTTPException(status_code=401, detail="未授权：token 缺失或不匹配")

    base = os.path.abspath(settings.output_dir)
    full = os.path.abspath(os.path.join(base, path))
    if not full.startswith(base + os.sep) or not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="未找到")
    return FileResponse(
        full, media_type=mimetypes.guess_type(full)[0] or "application/octet-stream"
    )


@app.get("/")
async def root():
    """根路径"""
    return {
        "name": "古诗词短视频工厂",
        "version": "0.1.0",
        "status": "running",
    }


@app.get("/health")
async def health():
    """健康检查"""
    return {"status": "healthy"}


def mask_api_key(key: str) -> tuple[str, bool]:
    """对 API 密钥脱敏回显（安全加固 REQ-S1）。

    返回 (脱敏字符串, 是否已配置)。空 → ("", False)；短 key(≤8) 只留首字符；
    正常 key 保留前 5 + 尾 4，中段以 **** 遮蔽，避免明文泄露到前端。
    """
    if not key:
        return "", False
    if len(key) <= 8:
        return f"{key[:1]}****", True
    return f"{key[:5]}****{key[-4:]}", True


@app.get("/api/settings")
async def get_settings(token: str = Depends(require_token)):
    """获取当前配置（已在启动期与 DB 合并；密钥字段脱敏回显）"""
    text_masked, text_cfg = mask_api_key(settings.text_api_key)
    image_masked, image_cfg = mask_api_key(settings.image_api_key)
    video_masked, video_cfg = mask_api_key(settings.video_api_key)
    return {
        # 文本模型（密钥脱敏：不回显明文，仅给 configured 标记）
        "text_api_key": text_masked,
        "text_api_key_configured": text_cfg,
        "text_base_url": settings.text_base_url,
        "text_model": settings.text_model,
        "text_concurrency": settings.text_concurrency,
        # 图片模型
        "image_api_key": image_masked,
        "image_api_key_configured": image_cfg,
        "image_base_url": settings.image_base_url,
        "image_model": settings.image_model,
        "image_concurrency": settings.image_concurrency,
        # 视频模型
        "video_api_key": video_masked,
        "video_api_key_configured": video_cfg,
        "video_base_url": settings.video_base_url,
        "video_model": settings.video_model,
        "video_concurrency": settings.video_concurrency,
        # 通用
        "critic_concurrency": settings.critic_concurrency,
        "script_score_threshold": settings.script_score_threshold,
        "image_score_threshold": settings.image_score_threshold,
        "max_retries": settings.max_retries,
        # TTS / 字幕并发
        "tts_concurrency": settings.tts_concurrency,
        "subtitle_concurrency": settings.subtitle_concurrency,
        # 多平台输出（设置页"发布平台"多选）
        "output_platforms": settings.output_platforms,
        # 水印配置（设置页"水印配置"区块）
        "watermark_enabled": settings.watermark_enabled,
        "watermark_text": settings.watermark_text,
        "watermark_fontsize_ratio": settings.watermark_fontsize_ratio,
        "watermark_colour": settings.watermark_colour,
        "watermark_alpha": settings.watermark_alpha,
        "watermark_margin_ratio": settings.watermark_margin_ratio,
        "watermark_position": settings.watermark_position,
        # 写库白名单（前端可写哪些字段）
        "writable_keys": list(WRITABLE_KEYS),
    }


@app.post("/api/settings")
async def update_settings(data: dict, token: str = Depends(require_token)):
    """更新配置：落库 + 热更新内存中的 Pydantic 实例。

    修复点（vs 旧版）：
      - 旧版仅 ``setattr`` 内存，重启即丢；现在 ``save_config_to_db`` 会
        序列化为 JSON 写 ``system_settings`` 表，下次启动由 lifespan 读回。
      - 同时白名单校验防止非法字段污染实例（WRITABLE_KEYS）。
    """
    payload = await save_config_to_db(data)
    apply_overlay(settings, payload)
    return {
        "message": "配置已保存并持久化",
        "saved_keys": list(payload.keys()),
        "settings": await get_settings(),
    }


@app.post("/api/settings/reset")
async def reset_settings_api(token: str = Depends(require_token)):
    """清空持久化配置，恢复 Pydantic 默认值。"""
    await reset_config_in_db()
    # 把内存中所有白名单字段恢复为默认值（仅对默认值常量有副本的字段；当前
    # 在 GET/POST 同步逻辑下，重新加载 = 重新按空 overlay 应用一次即可让
    # DB 不再覆盖默认）。
    for key in WRITABLE_KEYS:
        try:
            current = getattr(settings, key, None)
            # 反射回到 Pydantic 的默认值
            field = settings.__class__.model_fields.get(key)
            if field is not None:
                setattr(settings, key, field.default)
        except Exception as exc:
            logger.warning("重置设置 %s 失败: %s", key, exc)
    return {"message": "已恢复默认值", "settings": await get_settings()}


@app.get("/api/queue/status")
async def queue_status(token: str = Depends(require_token)):
    """生产队列实时状态（手动观察：pending / running / done / failed 任务分桶）。

    用于排查"为什么没在跑"——某阶段是不是一直 pending 看 Task 产物是否齐
    备（D5 死锁）/ 视频是不是等待 62s 间隔（D6 修复）。
    """
    from sqlalchemy import func as sa_func, select as sa_select
    from app.database import async_session_factory
    from app.models.job import Job

    async with async_session_factory() as session:
        rows = await session.execute(
            sa_select(Job.stage, Job.status, sa_func.count(Job.id)).group_by(Job.stage, Job.status)
        )
        bucket: dict[str, dict[str, int]] = {}
        for stage, status, count in rows.all():
            bucket.setdefault(stage, {})[status] = count
    return {
        "buckets": bucket,
        "video_min_interval_sec": settings.video_min_interval,
        "concurrency": {
            "text": settings.text_concurrency,
            "image": settings.image_concurrency,
            "video": settings.video_concurrency,
            "tts": settings.tts_concurrency,
            "subtitle": settings.subtitle_concurrency,
        },
        "throttle": _throttle_snapshot(),
    }


def _throttle_snapshot() -> dict:
    """4 个限流器的当前使用快照（D4 节流内核可观测性）。"""
    from app.services.rate_limiter import (
        text_limiter, image_1k_limiter, image_high_limiter, video_limiter,
    )
    return {
        l.name: l.snapshot()
        for l in (text_limiter, image_1k_limiter, image_high_limiter, video_limiter)
    }


@app.get("/api/settings/test-concurrency")
async def test_concurrency(
    type: str = Query(..., description="测试类型: text/image/video"),
    concurrency: int = Query(1, ge=1, le=10, description="并发数"),
    token: str = Depends(require_token),
):
    """测试并发数：同时发 N 个请求并统计成功/失败/耗时。

    修复（vs 旧版）：
      - 旧版硬编码 ``settings.agnes_base_url`` / ``settings.agnes_api_key``，但 Pydantic
        的字段其实是 ``text_base_url``/``image_base_url``/``video_base_url``，导致旧版
        实际全报异常（success=0）。
      - 视频参数用 SKILL 规范的 ``width/height/num_frames/frame_rate``，不再使用
        ``mode/seconds/size/aspect_ratio``（该旧字段在 agnes-video-v2.0 上被拒）。
      - 读当前用户在“系统设置”页面保存的对应类型 base_url/api_key/model。
      安全加固：接口限频（满窗 429），防止被刷消耗额度。
    """
    from app.services.rate_limiter import api_limiter_heavy

    if not await api_limiter_heavy.acquire(timeout=0):
        raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")

    import httpx

    if type == "text":
        base_url, api_key, model = settings.text_base_url, settings.text_api_key, settings.text_model
        endpoint = f"{base_url}/chat/completions"
        body = {"model": model, "messages": [{"role": "user", "content": "Say OK"}], "max_tokens": 5}
    elif type == "image":
        base_url, api_key, model = settings.image_base_url, settings.image_api_key, settings.image_model
        endpoint = f"{base_url}/images/generations"
        # 与 SKILL 规范一致：size=1K + ratio=1:1
        body = {"model": model, "prompt": "a simple test image",
                "size": "1K", "ratio": "1:1", "n": 1,
                "extra_body": {"response_format": "url"}}
    elif type == "video":
        base_url, api_key, model = settings.video_base_url, settings.video_api_key, settings.video_model
        endpoint = f"{base_url}/videos"
        # SKILL 规范：width/height/num_frames(8n+1)/frame_rate
        body = {"model": model, "prompt": "a simple test video",
                "width": 1152, "height": 768, "num_frames": 121, "frame_rate": 24}
    else:
        return {"error": f"未知类型: {type}"}

    results = []
    start_time = time.time()

    async def single_request(index: int):
        req_start = time.time()
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    endpoint,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=body,
                )
            elapsed = time.time() - req_start
            ok = resp.status_code == 200
            return {
                "index": index,
                "success": ok,
                "status_code": resp.status_code,
                "elapsed": round(elapsed, 2),
                "error": None if ok else (resp.text[:200] if not ok else None),
            }
        except Exception as e:
            elapsed = time.time() - req_start
            return {"index": index, "success": False, "error": str(e),
                    "elapsed": round(elapsed, 2)}

    tasks_list = [single_request(i) for i in range(concurrency)]
    results = await asyncio.gather(*tasks_list)

    total_time = time.time() - start_time
    success_count = sum(1 for r in results if r["success"])
    fail_count = concurrency - success_count

    return {
        "type": type,
        "concurrency": concurrency,
        "endpoint": endpoint,
        "model": model,
        "success": success_count,
        "failed": fail_count,
        "total_time": round(total_time, 2),
        "details": results,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
