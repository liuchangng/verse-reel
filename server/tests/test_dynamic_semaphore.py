"""DynamicSemaphore（并发配置热更）守护测试

背景：旧版用 asyncio.Semaphore 在启动时把 settings.*_concurrency 固化，
设置页改并发后不重启不生效（2026-09-09 问题 2/3/4/5）。
DynamicSemaphore 每次 locked()/acquire() 实时读 getter，改配置即生效。
"""
import asyncio

from app.services.queue import DynamicSemaphore


def test_acquire_release_basic():
    limit = {"v": 2}
    sem = DynamicSemaphore(lambda: limit["v"])

    async def main():
        await sem.acquire()
        await sem.acquire()
        assert sem.locked(), "达到上限 2 后应 locked"
        sem.release()
        assert not sem.locked(), "释放一个后不应 locked"

    asyncio.run(main())


def test_locked_reflects_raised_limit_immediately():
    """配置调大后 locked() 立即反映新值（旧 Semaphore 做不到）。"""
    limit = {"v": 1}
    sem = DynamicSemaphore(lambda: limit["v"])

    async def main():
        await sem.acquire()
        assert sem.locked()
        limit["v"] = 2  # 热更：并发 1 → 2
        assert not sem.locked(), "配置调大后应立即解锁新名额"

    asyncio.run(main())


def test_waiter_enters_after_limit_raised():
    """等待者被唤醒后按新配置放行（热更核心语义）。"""
    limit = {"v": 1}
    sem = DynamicSemaphore(lambda: limit["v"])

    async def main():
        await sem.acquire()
        waiter = asyncio.create_task(sem.acquire())
        await asyncio.sleep(0.01)
        assert not waiter.done(), "并发 1 已满，等待者不应进入"
        limit["v"] = 2   # 等待期间调大配置
        sem.release()    # 唤醒等待者
        await asyncio.wait_for(waiter, timeout=1.0), "唤醒后应按新配置放行"

    asyncio.run(main())


def test_waiter_requeues_when_slot_taken():
    """被唤醒但唯一名额被别人抢走时，应重新排队而不是越权进入。"""
    limit = {"v": 1}
    sem = DynamicSemaphore(lambda: limit["v"])

    async def main():
        await sem.acquire()
        w1 = asyncio.create_task(sem.acquire())
        w2 = asyncio.create_task(sem.acquire())
        await asyncio.sleep(0.01)
        assert not w1.done() and not w2.done()
        sem.release()  # current=0，唤醒两个等待者竞争唯一名额
        await asyncio.wait_for(w1, 1.0)  # w1 抢到（current=1，再次锁满）
        await asyncio.sleep(0.01)
        assert not w2.done(), "名额被 w1 抢走，w2 应继续排队等待"
        sem.release()
        await asyncio.wait_for(w2, 1.0), "再次释放后 w2 应进入"

    asyncio.run(main())
