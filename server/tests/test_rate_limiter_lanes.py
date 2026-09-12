"""限流器泳道隔离 + 队头阻塞修复 守护测试（2026-09-12）

背景：
- 队头阻塞：旧版 ``acquire`` 在 ``async with self._lock`` 内 ``await sleep(wait)``，
  锁被持有整个等待期，其余请求全部被堵在门外（18 RPM 下第 20 个请求要等约 66s，
  且期间无人能进入临界区）。修复：临界区内判定，释放锁后再 sleep。
- 泳道隔离：文本总量切出 interactive 预留配额给"人在等"的操作（热点页推荐），
  批量泳道只吃剩余额度，避免交互被批量饿死；两泳道合计不超过 text_rpm。
"""
import asyncio
import time

import pytest

import app.services.rate_limiter as rl
from app.services.rate_limiter import RateLimiter, get_text_limiter


def test_lane_routing():
    """泳道路由：interactive → 预留限流器；batch/None → 批量限流器。"""
    assert get_text_limiter("interactive") is rl.text_limiter_interactive
    assert get_text_limiter("batch") is rl.text_limiter
    assert get_text_limiter(None) is rl.text_limiter
    assert get_text_limiter("unknown-lane") is rl.text_limiter


def test_reserved_budget(monkeypatch):
    """批量泳道额度 = text_rpm - 交互预留；交互泳道额度 = 预留值。"""
    monkeypatch.setattr(rl.settings, "text_rpm", 18)
    monkeypatch.setattr(rl.settings, "text_rpm_interactive", 4)
    assert rl._batch_text_rpm() == 14
    assert rl.text_limiter.snapshot()["rpm_limit"] == 14
    assert rl.text_limiter_interactive.snapshot()["rpm_limit"] == 4


@pytest.mark.asyncio()
async def test_acquire_does_not_block_others():
    """队头阻塞修复：一个请求在等待窗口时，不得阻塞其他请求进入临界区。

    旧版缺陷下，等待者 sleep 期间持锁，``acquire(timeout=0)`` 会被堵在门外直到其
    醒来（本用例会 TimeoutError）；修复后满窗请求立即返回 False。
    """
    lim = RateLimiter(rpm=1, window_sec=0.3, name="t")
    assert await lim.acquire() is True          # 填满窗口

    waiter = asyncio.create_task(lim.acquire())  # 进入等待窗口
    await asyncio.sleep(0.02)                    # 让它进入 sleep（旧版此刻持锁）

    t0 = time.monotonic()
    ok = await asyncio.wait_for(lim.acquire(timeout=0), timeout=0.15)
    elapsed = time.monotonic() - t0
    assert ok is False, "满窗 + timeout=0 应立即拒绝"
    assert elapsed < 0.12, f"被队头阻塞（耗时 {elapsed:.3f}s），锁未在 sleep 前释放"

    waiter.cancel()
    try:
        await waiter
    except asyncio.CancelledError:
        pass
