"""队列消费进程级单实例锁 守护测试（2026-09-10 并发加固 P0-B）

为什么需要：``QueueService`` 只是内存单例，"全局只有一个消费者"此前靠
「lifespan 只跑一次」这个假设成立。以下场景都会打破它：

- ``start.bat`` 用 ``uvicorn --reload``：重启窗口内新旧子进程并存；
- 误起第二个实例（换端口即可绕过 8000 占用）；
- 测试用 ``TestClient(app)`` 触发 lifespan（2026-09-09 真实发生过）。

两个消费者同时扫 pending 会导致同一 Job 被派发两次、"任务间串行"失效。
本文件锁定：拿不到锁的进程只提供 API，不启动消费者；陈旧锁可被回收。
"""
import asyncio
import os
import time

import pytest

import app.services.queue as qmod
from app.services.queue import QueueService


@pytest.fixture()
def lock_file(monkeypatch, tmp_path):
    """把锁路径重定向到临时目录，避免测试污染 data 目录。"""
    path = tmp_path / ".queue.lock"
    monkeypatch.setattr(qmod, "_lock_path", lambda: str(path))
    return path


def _quiet(q: QueueService, monkeypatch):
    """让 worker loop 空转（不真扫库、不真跑流水线）。"""
    async def fake_claim():
        return 0

    async def fake_worker():
        await asyncio.sleep(3600)  # 被 stop() cancel

    monkeypatch.setattr(q, "_claim_and_dispatch", fake_claim)
    monkeypatch.setattr(q, "_worker_loop", fake_worker)


@pytest.mark.asyncio()
async def test_only_one_consumer_holds_the_lock(lock_file, monkeypatch):
    """第二个实例拿不到锁 → 不启动消费者（_leader=False，无 worker task）。"""
    q1 = QueueService()
    _quiet(q1, monkeypatch)
    await q1.start()
    assert q1._leader is True, "首个实例应取得消费权"
    assert q1._task is not None

    q2 = QueueService()
    _quiet(q2, monkeypatch)
    await q2.start()
    assert q2._leader is False, "锁被持有时不得再起一个消费者"
    assert q2._task is None, "非持有者不得创建 worker loop"

    try:
        assert lock_file.read_text(encoding="utf-8").strip() == str(os.getpid())
        # 释放后锁文件应被删除，下一个实例可以接管
        await q1.stop()
        assert not lock_file.exists(), "stop() 应释放锁文件"

        q3 = QueueService()
        _quiet(q3, monkeypatch)
        await q3.start()
        assert q3._leader is True, "锁释放后应可被重新取得"
        await q3.stop()
    finally:
        await q1.stop()
        await q2.stop()


@pytest.mark.asyncio()
async def test_stale_lock_is_reclaimed(lock_file, monkeypatch):
    """陈旧锁（持有进程已死）不阻塞启动：自动回收并写入当前 PID。"""
    dead_pid = 9_999_999  # 远超 Windows PID 上限，必然不存在
    assert not qmod._pid_alive(dead_pid)
    lock_file.write_text(str(dead_pid), encoding="utf-8")
    old = time.time() - 10_000
    os.utime(lock_file, (old, old))

    q = QueueService()
    _quiet(q, monkeypatch)
    await q.start()
    try:
        assert q._leader is True, "陈旧锁应被回收，而不是永久占着"
        assert lock_file.read_text(encoding="utf-8").strip() == str(os.getpid())
    finally:
        await q.stop()


@pytest.mark.asyncio()
async def test_heartbeat_stalled_lock_is_reclaimable(lock_file, monkeypatch):
    """PID 存活但心跳停摆超过阈值 → 视为僵死，允许抢占（防崩溃后永久占锁）。"""
    lock_file.write_text(str(os.getpid()), encoding="utf-8")  # 存活 PID = 自己
    old = time.time() - (qmod._LOCK_HEARTBEAT_SEC + 60)
    os.utime(lock_file, (old, old))  # 心跳停在阈值之外，模拟消费者僵死

    # 反例：正常心跳（mtime 新鲜）时不可抢占 —— 由 test_only_one_consumer_holds_the_lock 覆盖
    qmod._touch_instance_lock()
    assert time.time() - os.path.getmtime(lock_file) < qmod._LOCK_HEARTBEAT_SEC
    os.utime(lock_file, (old, old))  # 再次拨回，回到僵死状态

    q = QueueService()
    _quiet(q, monkeypatch)
    await q.start()
    try:
        assert q._leader is True, "心跳停摆超阈值应可被抢占"
    finally:
        await q.stop()
