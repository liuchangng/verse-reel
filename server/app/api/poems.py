"""诗词查询 API"""
import logging
from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.database import get_db
from app.models.poem import Poem
from app.models.task import Task

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/")
async def list_poems(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    search: Optional[str] = Query(None, description="搜索关键词"),
    dynasty: Optional[str] = Query(None, description="朝代筛选"),
    genre: Optional[str] = Query(None, description="体裁筛选"),
    status: Optional[str] = Query(None, description="状态筛选"),
    db: AsyncSession = Depends(get_db),
):
    """
    获取诗词列表
    
    支持按关键词、朝代、体裁筛选，分页返回
    """
    query = select(Poem)
    count_query = select(func.count(Poem.id))
    
    # 应用筛选条件
    if search:
        search_filter = Poem.title.contains(search) | Poem.author.contains(search)
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)
    
    if dynasty:
        query = query.where(Poem.dynasty == dynasty)
        count_query = count_query.where(Poem.dynasty == dynasty)
    
    if genre:
        query = query.where(Poem.genre == genre)
        count_query = count_query.where(Poem.genre == genre)
    
    # 获取总数
    total = await db.execute(count_query)
    total = total.scalar()
    
    # 分页查询
    offset = (page - 1) * page_size
    query = query.offset(offset).limit(page_size).order_by(Poem.id)
    result = await db.execute(query)
    poems = result.scalars().all()
    
    # 批量查询哪些诗词已有任务
    poem_ids = [p.id for p in poems]
    has_task_result = await db.execute(
        select(Task.poem_id).where(Task.poem_id.in_(poem_ids)).distinct()
    )
    poems_with_tasks = set(has_task_result.scalars().all())
    
    return {
        "items": [
            {
                "id": poem.id,
                "title": poem.title,
                "author": poem.author,
                "dynasty": poem.dynasty,
                "genre": poem.genre,
                "content_preview": poem.content[:100] + "..." if len(poem.content or "") > 100 else poem.content,
                "content": poem.content,
                "title_traditional": poem.title_traditional,
                "has_task": poem.id in poems_with_tasks,
            }
            for poem in poems
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
    }


@router.get("/stats")
async def get_stats(db: AsyncSession = Depends(get_db)):
    """获取诗词统计信息"""
    total = (await db.execute(select(func.count(Poem.id)))).scalar() or 0
    
    # 朝代分布
    dynasty_rows = (await db.execute(
        select(Poem.dynasty, func.count(Poem.id).label('cnt'))
        .group_by(Poem.dynasty)
        .order_by(func.count(Poem.id).desc())
        .limit(10)
    )).all()
    
    # 体裁分布
    genre_rows = (await db.execute(
        select(Poem.genre, func.count(Poem.id).label('cnt'))
        .group_by(Poem.genre)
        .order_by(func.count(Poem.id).desc())
        .limit(10)
    )).all()
    
    # 任务统计
    total_tasks = (await db.execute(select(func.count(Task.id)))).scalar() or 0
    processing_tasks = (await db.execute(
        select(func.count(Task.id)).where(Task.status == "processing")
    )).scalar() or 0
    completed_tasks = (await db.execute(
        select(func.count(Task.id)).where(Task.status.in_(["completed", "done"]))
    )).scalar() or 0
    
    return {
        "total": total,
        "total_tasks": total_tasks,
        "processing_tasks": processing_tasks,
        "completed_tasks": completed_tasks,
        "dynasty_distribution": [{"dynasty": d, "count": c} for d, c in dynasty_rows],
        "genre_distribution": [{"genre": g, "count": c} for g, c in genre_rows],
    }


@router.get("/{poem_id}")
async def get_poem(poem_id: int, db: AsyncSession = Depends(get_db)):
    """获取单个诗词详情"""
    poem = await db.get(Poem, poem_id)
    if not poem:
        raise HTTPException(status_code=404, detail="诗词不存在")
    
    return {
        "id": poem.id,
        "title": poem.title,
        "title_traditional": poem.title_traditional,
        "author": poem.author,
        "author_traditional": poem.author_traditional,
        "dynasty": poem.dynasty,
        "genre": poem.genre,
        "content": poem.content,
        "content_traditional": poem.content_traditional,
        "rhyme": poem.rhyme,
    }
