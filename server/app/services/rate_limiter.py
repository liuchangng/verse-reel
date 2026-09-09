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

from app.config import settings

logger = logging.getLogger(__name__)


class RateLimiter:
    """单类型滑动窗口 RPM 限流器。

    Args:
        rpm: 每分钟最大请求数（已为官方限值的 90%，留有 headroom）。
             也是 ``rpm_getter`` 缺省时的回退值。
        window_sec: 窗口大小（秒），默认 60。
        name: 用于日志的标签（"text" / "image-1K" / "video"）。
        rpm_getter: 可选，实时读取当前 RPM 的回调——设置页改 RPM 后
            不重启即生效（2026-09-09 问题 2-5：限流参数随配置热更）。
    """

    def __init__(
        self,
        rpm: int,
        window_sec: float = 60.0,
        name: str = "?",
        rpm_getter=None,
    ):
        self._initial_rpm = max(1, rpm)
        self._rpm_getter = rpm_getter
        self.window_sec = window_sec
        self.name = name
        # 每次请求进入的时间戳；容量由 _current_rpm() 动态判定，
        # 超窗时间戳在 acquire 循环里滑出（不再用 maxlen 固定容量）。
        self._hits: deque[float] = deque()
        self._lock = asyncio.Lock()
        # 统计
        self.wait_count = 0
        self.served = 0
        self.denied_429 = 0

    def _current_rpm(self) -> int:
        """实时 RPM：优先 rpm_getter（配置热更），异常/缺省回退初始值。"""
        if self._rpm_getter is not None:
            try:
                v = int(self._rpm_getter())
                if v >= 1:
                    return v
            except (TypeError, ValueError):
                pass
        return self._initial_rpm

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
                # 每轮实时读配置（热更后新限值立即生效）
                rpm = self._current_rpm()
                if len(self._hits) < rpm:
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
            "rpm_limit": self._current_rpm(),
            "window_sec": self.window_sec,
            "used_in_window": len(self._hits),
            "served": self.served,
            "denied_429": self.denied_429,
            "wait_count": self.wait_count,
        }


# ---- 全局实例（按类型分配；90% 官方限值的初始值，rpm_getter 实时读设置热更）----
# 文本：官方 30 RP0，实际 20 → 18
text_limiter = RateLimiter(
    rpm=18, name="text",
    rpm_getter=lambda: getattr(settings, "text_rpm", 18),
)
# 图片 1K：官方 30，实际 20 → 18
image_1k_limiter = RateLimiter(
    rpm=18, name="image-1K",
    rpm_getter=lambda: getattr(settings, "image_1k_rpm", 18),
)
# 图片 2K/3K：更紧
image_high_limiter = RateLimiter(
    rpm=8, name="image-2K+",
    rpm_getter=lambda: getattr(settings, "image_high_rpm", 8),
)
# 视频：官方 2，实际 1 → 1（不打折，单数）
video_limiter = RateLimiter(
    rpm=1, name="video",
    rpm_getter=lambda: getattr(settings, "video_rpm", 1),
)

# ---- 接口层限频（安全加固 REQ-S3：防刷/防滥用，非外部 API 节流）----
# 用法：await limiter.acquire(timeout=0) —— 满窗立即返回 False → 接口回 429（只拒不等待）
api_limiter_general = RateLimiter(rpm=60, name="api-general")        # 兜底（未细分接口）
api_limiter_heavy = RateLimiter(rpm=10, name="api-heavy")            # test-concurrency / batch/generate
api_limiter_destructive = RateLimiter(rpm=3, name="api-destructive") # batch-clear 破坏性操作


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
    "api_limiter_general",
    "api_limiter_heavy",
    "api_limiter_destructive",
]
