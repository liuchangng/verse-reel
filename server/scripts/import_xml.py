"""XML 数据导入脚本 - 将 CNKGraph XML 导入 SQLite"""
import sys
import time
import logging
from pathlib import Path
from typing import Optional

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config import settings
from app.database import Base
from app.models.poem import Poem
from app.services.xml_parser import parse_xml_file

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def print_progress(current: int, total: int, start_time: float, bar_length: int = 40):
    """打印进度条"""
    elapsed = time.time() - start_time
    rate = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / rate if rate > 0 else 0
    
    # 进度条
    progress = current / total if total > 0 else 0
    filled_length = int(bar_length * progress)
    bar = '█' * filled_length + '░' * (bar_length - filled_length)
    
    # 格式化时间
    elapsed_str = f"{int(elapsed)}s"
    eta_str = f"{int(eta)}s" if eta < 3600 else f"{int(eta/3600)}h{int((eta%3600)/60)}m"
    
    print(f"\r  [{bar}] {progress*100:.1f}% | {current}/{total} | {rate:.0f}/s | ETA: {eta_str}", end='', flush=True)


async def import_data(file_path: str, batch_size: int = 1000):
    """
    导入 XML 数据到 SQLite
    
    Args:
        file_path: XML 文件路径
        batch_size: 批次大小
    """
    # 创建数据库连接
    engine = create_async_engine(settings.database_url, echo=False)
    async_session = async_sessionmaker(engine, expire_on_commit=False)
    
    # 创建表
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    logger.info(f"开始导入: {file_path}")
    start_time = time.time()
    total_imported = 0
    total_skipped = 0
    
    # 先统计总数（快速）
    print("正在统计诗词总数...")
    total_count = 0
    for batch in parse_xml_file(file_path, batch_size=10000):
        total_count += len(batch)
    print(f"诗词总数: {total_count:,}")
    print()
    
    # 解析并导入
    imported_ids = set()
    async with async_session() as session:
        # 已导入的 ID
        result = await session.execute(select(Poem.cnk_id))
        imported_ids = set(result.scalars().all())
        print(f"数据库已有: {len(imported_ids):,} 首")
        print()
        
        for batch in parse_xml_file(file_path, batch_size=batch_size):
            for poem_data in batch:
                try:
                    # 检查是否已存在
                    if poem_data.cnk_id in imported_ids:
                        total_skipped += 1
                        continue
                    
                    # 创建新记录
                    poem = Poem(
                        cnk_id=poem_data.cnk_id,
                        title=poem_data.title,
                        title_traditional=poem_data.title_traditional,
                        author=poem_data.author,
                        author_traditional=poem_data.author_traditional,
                        dynasty=poem_data.dynasty,
                        genre=poem_data.genre,
                        content=poem_data.content,
                        content_traditional=poem_data.content_traditional,
                        rhyme=poem_data.rhyme,
                    )
                    session.add(poem)
                    imported_ids.add(poem_data.cnk_id)
                    total_imported += 1
                    
                except Exception as e:
                    logger.warning(f"导入诗词 {poem_data.cnk_id} 失败: {e}")
                    total_skipped += 1
            
            # 批量提交
            await session.commit()
            
            # 打印进度
            processed = total_imported + total_skipped
            if processed % 1000 == 0 or processed == total_count:
                print_progress(processed, total_count, start_time)
    
    # 关闭连接
    await engine.dispose()
    
    elapsed = time.time() - start_time
    print()
    print()
    logger.info(f"导入完成!")
    logger.info(f"  - 新增导入: {total_imported:,} 首")
    logger.info(f"  - 跳过重复: {total_skipped:,} 首")
    logger.info(f"  - 耗时: {elapsed:.2f} 秒")
    logger.info(f"  - 平均速度: {total_imported/elapsed:.0f} 首/秒")


async def show_stats():
    """显示数据库统计"""
    engine = create_async_engine(settings.database_url, echo=False)
    async_session = async_sessionmaker(engine, expire_on_commit=False)
    
    async with async_session() as session:
        # 总数
        total = await session.execute(select(func.count(Poem.id)))
        total = total.scalar()
        
        # 按朝代统计
        dynasty_stats = await session.execute(
            select(Poem.dynasty, func.count(Poem.id))
            .group_by(Poem.dynasty)
            .order_by(func.count(Poem.id).desc())
            .limit(10)
        )
        
        # 按体裁统计
        genre_stats = await session.execute(
            select(Poem.genre, func.count(Poem.id))
            .group_by(Poem.genre)
            .order_by(func.count(Poem.id).desc())
            .limit(10)
        )
    
    await engine.dispose()
    
    print()
    print("=" * 50)
    print("  数据库统计")
    print("=" * 50)
    print(f"  诗词总数: {total:,}")
    print()
    
    print("  朝代分布 (Top 10):")
    for dynasty, count in dynasty_stats:
        print(f"    {dynasty or '未知':10} {count:>10,}")
    
    print()
    print("  体裁分布 (Top 10):")
    for genre, count in genre_stats:
        print(f"    {genre or '未知':10} {count:>10,}")
    print()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="导入 CNKGraph XML 数据")
    parser.add_argument(
        "--file",
        default=settings.xml_file_path,
        help="XML 文件路径"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="批次大小"
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="显示数据库统计"
    )
    
    args = parser.parse_args()
    
    if args.stats:
        import asyncio
        asyncio.run(show_stats())
    else:
        import asyncio
        asyncio.run(import_data(args.file, args.batch_size))
