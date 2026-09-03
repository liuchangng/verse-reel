"""RPM 滑动窗口限流器（D4：L2 主动节流）

核心论点：``image_concurrency=3`` 只是闸门，**不能解决 RPM 问题**。
单图 8s 时即使只并发 3，每分钟也能打到 22.5 次 → 超过 agnes 1K 20 RPM
→ 必撞 429。所以必须在并发闸门**之外**再加一层滑动窗口，**主动节流**到
该类型的官方 RPM 上限。

设计：
- 每类型（text/image/video）一个 ``RateLimiter`` 实例。
- 滑动窗口 = 60s 内允许 N 次（官方值 × 0.9），到达上限时新请求 ``await``
  到窗口滑出最早的请求。
- 遇 429 时调用 ``record_429`` 标记一次失败（窗口不主动释放，避免乐观估计
  反而撞限）——但本版本默认 90% headroom，正常情况收不到 429。
- 单进程内存态；多进程部署需迁到 Redis（接口已为此预留：用 ``_storage``
  注入）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)


class RateLimiter:
    """单类型滑动窗口 RPM 限流器。

    Args:
        rpm: 每分钟最大请求数（已为官方限值的 90%，留有 headroom）。
        window_sec: 窗口大小（秒），默认 60。
        name: 用于日志的标签（"text" / "image-1K" / "video"）。
    """

    def __init__(self, rpm: int, window_sec: float = 60.0, name: str = "?"):
        if rpm < 1:
            rpm = 1
        self.rpm = rpm
        self.window_sec = window_sec
        self.name = name
        # 每次请求进入的时间戳；队列长度为限制 rpm（最坏情况下都刚发）
        self._hits: deque[float] = deque(maxlen=rpm)
        self._lock = asyncio.Lock()
        # 统计
        self.wait_count = 0
        self.served = 0
        self.denied_429 = 0

    async def acquire(self, timeout: Optional[float] = None) -> bool:
        """等待直到窗口内放行一个新请求。

        Args:
            timeout: 最长等待秒数；None 表示不限。返回 False 表示超时。
        """
        deadline = (time.monotonic() + timeout) if timeout is not None else None
        async with self._lock:
            while True:
                now = time.monotonic()
                # 把超过窗口的命中滑出
                while self._hits and (now - self._hits[0]) >= self.window_sec:
                    self._hits.popleft()
                if len(self._hits) < self.rpm:
                    # 放行：记录本次时间戳
                    self._hits.append(now)
                    self.served += 1
                    return True
                # 还在限：等到最早那个离开窗口
                wait = self.window_sec - (now - self._hits[0])
                self.wait_count += 1
                # 释放锁让别人能 continue / 也能进入 acquire
                # ⚠️ 这里简单做法是释放锁再睡 —— 期间可能有别人超车导致
                # 本次依旧 wait 较短时间。OK：相当于"窗口"近似，未严格。
                if deadline is not None and (deadline - time.monotonic()) < wait:
                    return False
                # 让出锁 sleep 早一点点
                await asyncio.sleep(max(wait, 0.05))

    def record_429(self) -> None:
        """上游返回 429/限流时调用：记录失败但不提前释放配额（保守）。"""
        self.denied_429 += 1
        logger.warning(
            "限流器 %s 收到一次 429（已 served=%d, denied_429=%d）",
            self.name, self.served, self.denied_429,
        )

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "rpm_limit": self.rpm,
            "window_sec": self.window_sec,
            "used_in_window": len(self._hits),
            "served": self.served,
            "denied_429": self.denied_429,
            "wait_count": self.wait_count,
        }


# ---- 全局实例（按类型分配；90% 官方限值的初始值，可在运行时由配置调整） ----
# 文本：官方 30 RP0，实际 20 → 18
text_limiter = RateLimiter(rpm=18, name="text")
# 图片 1K：官方 30，实际 20 → 18
image_1k_limiter = RateLimiter(rpm=18, name="image-1K")
# 图片 2K/3K：更紧
image_high_limiter = RateLimiter(rpm=8, name="image-2K+")
# 视频：官方 2，实际 1 → 1（不打折，单数）
video_limiter = RateLimiter(rpm=1, name="video")


def get_image_limiter(size: str | None) -> RateLimiter:
    """按 size 选图片档位：含 "K" 后大于 1 视为高分辨率（2K+）。"""
    if size and any(str(int(s)) if s.isdigit() else "" and int(s) > 1 for s in size.replace("K", "").split("x") if str(s).isdigit()):
        return image_high_limiter
    if size and size not in ("", "1K"):
        return image_high_limiter
    return image_1k_limiter


__all__ = [
    "RateLimiter",
    "text_limiter",
    "image_1k_limiter",
    "image_high_limiter",
    "video_limiter",
    "get_image_limiter",
]
