"""数据库连接和会话管理"""
import logging

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

logger = logging.getLogger(__name__)


# 创建异步引擎
engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    future=True,
)

# 创建异步会话工厂
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """模型基类"""
    pass


async def get_db() -> AsyncSession:
    """获取数据库会话（依赖注入）"""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db():
    """初始化数据库（创建表 + 补齐已存在表缺失的列）"""
    async with engine.begin() as conn:
        # 导入所有模型以确保它们被注册
        # poet 为诗人名望实体表（由 scripts/build_poets_etl.py 幂等重建，服务端只读）；
        # 必须在此导入，否则新环境部署时 create_all 不会建 poets 表。
        from app.models import (  # noqa: F401
            poem, task, script, poem_term, hotspot, job, system_setting, poet,
        )
        await conn.run_sync(Base.metadata.create_all)
    # 旧生产库的已存在表不会因 create_all 自动加列，这里自愈式补齐，
    # 否则新增模型列（如 REQ-2 审核字段）在读写时会报 'no such column'。
    await migrate_schema()


async def migrate_schema():
    """补齐已存在表缺失的列（Base.metadata.create_all 不会给旧表加列）。

    演进场景：新增模型列（如 REQ-2 的 review_status / review_comment / reviewed_at）
    后，旧生产库的 tasks 表不会自动加列，导致访问该列报
    ``no such column: tasks.review_status``。启动时按模型定义补齐，保证模型
    与库表一致。保持新列为 nullable 以兼容非空表（NOT NULL 无默认值会失败）。
    """
    from sqlalchemy import inspect as sa_inspect, text as sa_text

    async with engine.begin() as conn:
        def _migrate(sync_conn):
            inspector = sa_inspect(sync_conn)
            dialect = sync_conn.dialect
            for table_name, table in Base.metadata.tables.items():
                try:
                    existing = {c["name"] for c in inspector.get_columns(table_name)}
                except Exception:
                    # 表不存在：交给 create_all 建新表，这里跳过
                    continue
                for col in table.columns:
                    if col.name in existing:
                        continue
                    col_type = col.type.compile(dialect=dialect)
                    ddl = f"ALTER TABLE {table_name} ADD COLUMN {col.name} {col_type}"
                    if col.server_default is not None:
                        ddl += f" DEFAULT {col.server_default.compile(dialect=dialect)}"
                    logger.info("migrate: %s 补列 %s (%s)", table_name, col.name, col_type)
                    sync_conn.execute(sa_text(ddl))

        await conn.run_sync(_migrate)
