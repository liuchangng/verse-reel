"""热点 API"""
import logging
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Query, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.hotspot import hotspot_service
from app.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter()


async def _background_refresh(platform_list: list[str], limit: int):
    """后台异步刷新：抓取外部热搜 + LLM 推荐 + 覆盖写库。

    异常时静默降级（不抛到主线程），保证前台请求永远快速返回。
    """
    try:
        async with _get_db_ctx() as db:
            live = await hotspot_service.fetch_hotspots(platform_list, limit)
            enriched: dict[str, list[dict]] = {}
            for platform, items in live.items():
                enriched[platform] = []
                for item in items:
                    recs = await hotspot_service.recommend_poems(db, item)
                    enriched[platform].append({
                        "title": item.get("title", ""),
                        "hot": item.get("hot", 0),
                        "url": item.get("url", ""),
                        "recommended_poems": recs,
                        "_rec_ids": [r["id"] for r in recs],
                        # 兜底标记透传：save_hotspots 跳过静态数据，不覆盖库内真实抓取
                        "_is_fallback": item.get("_is_fallback", False),
                    })
            save_struct = {
                platform: [
                    {
                        "title": it["title"], "hot": it["hot"], "url": it["url"],
                        "recommended_poems": it["_rec_ids"],
                        "_is_fallback": it.get("_is_fallback", False),
                    }
                    for it in items
                ]
                for platform, items in enriched.items()
            }
            await hotspot_service.save_hotspots(db, save_struct)
            logger.info("后台热点刷新完成，已覆盖写库")
    except Exception as e:
        logger.error("后台热点刷新失败（不影响已返回的缓存数据）: %s", e)


# 简易 async context manager（兼容不同 SQLAlchemy 版本）
class _get_db_ctx:
    async def __aenter__(self):
        from app.database import async_session_factory
        self.session = async_session_factory()
        await self.session.begin()
        return self.session
    async def __aexit__(self, exc_type, *exc):
        # 关键: 显式事务需 commit 才落库, 否则 session.close() 隐式回滚导致写入丢失
        try:
            if exc_type is None:
                await self.session.commit()
            else:
                await self.session.rollback()
        finally:
            await self.session.close()


@router.get("/")
async def get_hotspots(
    platforms: str = Query("weibo,douyin,kuaishou,bilibili,zhihu,baidu", description="平台列表，逗号分隔"),
    limit: int = Query(20, ge=1, le=50, description="每平台返回数量"),
    force: bool = Query(False, description="强制重新抓取并覆盖写库（手动刷新用）"),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: AsyncSession = Depends(get_db),
):
    """获取多平台热搜数据（即时读库 + 过期后台刷新）

    策略：
    - 库有数据 → **立即返回** DB 缓存（< 100ms），不等待外部 API。
      若数据过期(>10min) 或 force=True，同时触发**后台异步刷新**写库，
      下次请求自动拿到新数据。
    - 库为空（首次使用）→ 同步阻塞抓取一次（必须等，否则无数据可返），
      写库后返回。之后所有请求都走上面的「即时读库」路径。

    Args:
        platforms: 平台列表，逗号分隔（weibo/douyin/kuaishou/bilibili/zhihu/baidu）
        limit: 每个平台返回数量
        force: True 时无视缓存立即重抓（手动刷新），但仍先返回旧数据
    """
    platform_list = [p.strip() for p in platforms.split(",") if p.strip()]

    # 1. 读库（始终执行，用于即时返回）
    cached, last_fetch = await hotspot_service.load_hotspots(db)

    # 2. 判断是否需要刷新
    need_refresh = (
        force
        or not cached
        or last_fetch is None
        or (datetime.now() - last_fetch) >= timedelta(minutes=10)
    )

    # 3. 库有数据 → 立即组装返回，需要时触发后台刷新
    if cached:
        # 即刻返回：用库内推荐 id 还原完整诗词对象
        display = await hotspot_service.hydrate_recommendations(db, cached)

        # 需要刷新？→ 后台异步执行，不阻塞响应
        if need_refresh:
            logger.info("DB 有缓存但%s，触发后台异步刷新" % (
                "强制刷新" if force else "已超过 10 分钟"))
            background_tasks.add_task(_background_refresh, platform_list, limit)

        # 组装结果
        result = _build_result(display, hotspot_service)
        top_poems = await hotspot_service.get_top_poems(db, limit=10)
        return {
            "hotspots": result,
            "total": len(result),
            "platforms": platform_list,
            "top_poems": top_poems,
            "refreshing": need_refresh,  # 前端可用于显示「更新中…」提示
        }

    # 4. 库为空（首次）→ 必须同步抓取（否则无数据可返）
    logger.info("热点库为空，同步抓取首次数据")
    live = await hotspot_service.fetch_hotspots(platform_list, limit)
    enriched: dict[str, list[dict]] = {}
    for platform, items in live.items():
        enriched[platform] = []
        for item in items:
            recs = await hotspot_service.recommend_poems(db, item)
            enriched[platform].append({
                "title": item.get("title", ""),
                "hot": item.get("hot", 0),
                "url": item.get("url", ""),
                "recommended_poems": recs,
                "_rec_ids": [r["id"] for r in recs],
            })
    save_struct = {
        platform: [
            {"title": it["title"], "hot": it["hot"], "url": it["url"],
             "recommended_poems": it["_rec_ids"]}
            for it in items
        ]
        for platform, items in enriched.items()
    }
    await hotspot_service.save_hotspots(db, save_struct)
    display = enriched

    result = _build_result(display, hotspot_service)
    top_poems = await hotspot_service.get_top_poems(db, limit=10)
    return {
        "hotspots": result,
        "total": len(result),
        "platforms": platform_list,
        "top_poems": top_poems,
        "refreshing": False,
    }


def _build_result(display: dict, svc) -> list[dict]:
    """从 hydrate/fetch 后的 display 字典组装统一结果列表"""
    result = []
    for platform, items in display.items():
        for item in items:
            themes = svc.match_themes({platform: [item]})
            keywords = svc.get_trending_keywords({platform: [item]})
            result.append({
                "platform": platform,
                "title": item.get("title", ""),
                "hot": item.get("hot", 0),
                "url": item.get("url", ""),
                "themes": themes,
                "keywords": keywords,
                "recommended_poems": item.get("recommended_poems", []),
            })
    return result
