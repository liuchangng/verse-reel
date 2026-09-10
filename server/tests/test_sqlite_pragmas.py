"""SQLite 并发 pragmas 守护测试（2026-09-10 并发加固 P1-C）

背景：engine 此前未设任何 PRAGMA，SQLite 默认 journal_mode=delete（rollback
journal），写事务锁整库、读写互斥。服务同时有四类读写来源（队列消费者循环、
pipeline 各阶段、REST API、WebSocket 每秒推进度），并发时会撞
``database is locked``。

本文件锁定：WAL 已开启、busy_timeout 已放宽到 15s。
"""
import pytest
from sqlalchemy import text

from app.database import _SQLITE_BUSY_TIMEOUT_MS, engine


@pytest.mark.asyncio()
async def test_journal_mode_is_wal():
    """journal_mode 必须是 WAL：读写不再互斥。"""
    async with engine.connect() as conn:
        mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
    assert str(mode).lower() == "wal", f"期望 WAL，实际 {mode}"


@pytest.mark.asyncio()
async def test_busy_timeout_is_raised():
    """busy_timeout 必须 ≥15s：写写冲突先等待而不是立刻抛 database is locked。"""
    async with engine.connect() as conn:
        timeout = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
    assert int(timeout) >= _SQLITE_BUSY_TIMEOUT_MS, f"期望 ≥{_SQLITE_BUSY_TIMEOUT_MS}，实际 {timeout}"
