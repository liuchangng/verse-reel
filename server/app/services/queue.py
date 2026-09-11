"""生成队列服务 - 生产者/消费者模式（DB 持久化，重启可恢复）

设计（对应需求讨论的方式二）：
- 文本/图片生成很快，视频很慢（agnes 视频硬限流 1 次/分钟）。
- 把文本+图片按批先生成（跨任务并发），视频按任务依次串行生成。
- 用数据库 generation_jobs 表做队列；后端重启后扫描孤儿 Job 继续执行。

阶段与并发：
- script/character/image/tts/subtitle 为「快速阶段」，优先级高；
- video 为「慢速阶段」，优先级低 + video_concurrency=1（串行）。
消费者按 (priority DESC, created_at ASC) 扫描 pending，且只在依赖阶段全部 done 后才认领。
"""
import asyncio
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session_factory
from app.models.job import Job
from app.models.task import Task
from app.services.pipeline import pipeline_engine

logger = logging.getLogger(__name__)

# 全局视频提交节流状态（D6/D7）：跨任务跨进程实例的所有 video Job 共享同一时间戳。
# 注意：当前是单进程内存态；如果未来做多 worker 部署，需要把这个时间戳搬到 Redis/DB。
_video_state_lock = asyncio.Lock()
_last_video_submitted_at: Optional[datetime] = None


def _mark_video_submitted():
    """由 _run_job 调用：登记一个 video 已提交（用于下个 video 间隔判断）。"""
    global _last_video_submitted_at
    _last_video_submitted_at = datetime.now(timezone.utc)


async def _load_job(job_id: int) -> Optional[Job]:
    """独立 session 读取 Job（用于视频节流预判，不阻塞 _run_job 的主事务）。"""
    async with async_session_factory() as session:
        return await session.get(Job, job_id)


# ------------------------------------------------------------------ #
# 进程级单实例锁（2026-09-10 并发加固 P0-B）
#
# QueueService 只是内存单例，"全局只有一个消费者"此前完全依赖「lifespan 只会跑
# 一次」这个假设。真实破坏该假设的场景都存在：
#   - start.bat 用 ``uvicorn --reload``：文件变更重启时新子进程已起、旧子进程未退，
#     两个 lifespan 各起一个消费者；
#   - 误起第二个实例（换端口即可绕过 8000 占用）；
#   - 测试用 ``TestClient(app)`` 触发 lifespan（2026-09-09 已真实发生，见
#     tests/test_queue_db_isolation.py 的事故复盘）。
# 后果：同一 Job 被派发两次，且"任务间串行"（内存约定）同时失效。
#
# 方案：锁文件 + PID 活性 + 心跳超时三重判定。
#   - 创建用 O_EXCL（原子），两进程同时抢占只有一个成功；
#   - 持有者每轮刷新 mtime 作心跳，崩溃/僵死超过阈值可被抢占；
#   - 进程已死（PID 不存在）立即回收，不会留下永久锁。
# 拿不到锁的进程仍然提供 API（可入队、可查询），只是不消费队列。
# ------------------------------------------------------------------ #
_LOCK_FILENAME = ".queue.lock"
_LOCK_HEARTBEAT_SEC = 120.0  # 心跳有效期：PID 存活但心跳停摆超时 → 视为陈旧锁


def _lock_path() -> str:
    """锁文件路径：与数据库同目录（测试可 monkeypatch 本函数改指向临时文件）。"""
    db_path = settings.database_url.split("///", 1)[-1]
    return os.path.join(os.path.dirname(db_path) or ".", _LOCK_FILENAME)


def _pid_alive(pid: int) -> bool:
    """进程存活判定（Windows 下 os.kill(pid, 0) 只检查存在性，不真正发信号）。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但无权限：保守视为存活，不抢占
    except OSError:
        return False
    return True


def _acquire_instance_lock() -> bool:
    """尝试获取消费者单实例锁；失败返回 False（本进程不得启动消费者）。"""
    path = _lock_path()
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    except OSError:
        pass

    try:
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    pid = int((f.read() or "0").strip() or 0)
            except (ValueError, OSError):
                pid = 0
            try:
                age = time.time() - os.path.getmtime(path)
            except OSError:
                age = float("inf")

            if _pid_alive(pid) and age < _LOCK_HEARTBEAT_SEC:
                logger.warning(
                    "⚠️ 队列锁被 pid=%s 持有（%.0fs 前心跳），本进程不启动消费者"
                    " —— 若确认无其他实例在跑，删除 %s 后重启",
                    pid, age, path,
                )
                return False

            logger.warning(
                "♻️ 回收陈旧队列锁（pid=%s 存活=%s，心跳 %.0fs 前）：%s",
                pid, _pid_alive(pid), age, path,
            )
            try:
                os.remove(path)
            except OSError:
                pass

        # O_EXCL 保证原子：并发抢占时只有一个进程能创建成功
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return True
    except FileExistsError:
        return False
    except OSError as e:
        logger.warning("队列锁获取失败（%s），本进程不启动消费者", e)
        return False


def _touch_instance_lock() -> None:
    """刷新心跳（持有者每轮调用）。"""
    try:
        if os.path.exists(_lock_path()):
            os.utime(_lock_path(), None)
    except OSError:
        pass


def _release_instance_lock() -> None:
    """释放锁（正常关闭时调用）。"""
    try:
        os.remove(_lock_path())
    except OSError:
        pass


# 标准阶段顺序（用于依赖判断与最终态判定）
STAGE_ORDER = ["script", "character", "image", "tts", "video", "subtitle"]

# 每个阶段的前置依赖（必须全部 done 才能认领）
STAGE_PREREQS = {
    "script": set(),
    "character": {"script"},
    "image": {"script", "character"},
    "tts": {"script"},
    "video": {"image"},
    "subtitle": {"video", "tts"},
}

# 消费优先级：快速阶段高、视频低（方式二批处理的核心）
STAGE_PRIORITY = {
    "script": 60,
    "character": 55,
    "image": 50,
    "tts": 45,
    "subtitle": 40,
    "video": 10,
}

# 每个阶段对应的 task 产物字段（用于「按阶段范围精确清空」）
# 旧实现 clear_outputs=True 会清空全部产物，导致「只重生成图片」把
# 文案 / 分镜 / 定妆照一起抹掉。改为只清空被重跑阶段自己的字段。
STAGE_OUTPUTS = {
    "script": ["script", "script_score", "storyboard", "style"],
    "character": ["character_ref", "character_description"],
    "image": ["image_urls", "image_score"],
    "tts": ["audio_url"],
    "video": ["video_url", "video_duration"],
    "subtitle": ["subtitle_url"],
}


def _expand_prereqs(stages: list[str], produced: dict[str, bool]) -> list[str]:
    """把用户指定的阶段补全为「含全部未满足前置」的阶段集合。

    问题背景：用户点「只重生成图片」，但任务的 ``character_ref`` 为空 ——
    ``STAGE_PREREQS['image'] = {script, character}`` 判定前置未满足，image
    Job 会永远卡在 pending。这里在入队时把缺失的前置阶段一并补上（递归），
    并按 ``STAGE_ORDER`` 排序保证执行顺序。

    Args:
        stages: 用户希望重跑的阶段
        produced: ``_task_produced(task)`` 的结果（**必须在清空产物之前计算**）

    Returns:
        补全并按 STAGE_ORDER 排序后的阶段列表
    """
    need = {s for s in stages if s in STAGE_PREREQS}
    changed = True
    while changed:
        changed = False
        for st in list(need):
            for pre in STAGE_PREREQS.get(st, set()):
                if pre not in need and not produced.get(pre, False):
                    need.add(pre)
                    changed = True
    return [s for s in STAGE_ORDER if s in need]


def _task_produced(task: Task) -> dict[str, bool]:
    """判断 task 已经生成了哪些阶段的产物（用于幂等依赖判断 / D5 死锁修复）。

    注意：产物字段存在并且"非空/非占位符"才视为产生过，避免 DB 默认空串/空
    JSON 误判。
    """
    def _nonempty(v) -> bool:
        if v is None:
            return False
        if isinstance(v, str):
            return v.strip() not in ("", "[]", "{}", "null")
        return bool(v)

    return {
        # script: 字符串正文；空字符串/默认占位符视为未生成
        "script": _nonempty(task.script),
        # character: 定妆照锚点
        "character": _nonempty(getattr(task, "character_ref", None)),
        # image: 只看分镜图本身。2026-09-09 任务001死循环事故：旧版写成
        # ``image_urls or character_ref``，定妆照在而分镜图空时 image 被
        # 误判"已产出"，_expand_prereqs 永不补建 image → video 三连失败
        # "缺分镜图" → 重试同一循环永无出口。character 阶段只写
        # character_ref，image_urls 是 image 阶段独有产物，不可混判。
        "image": _nonempty(task.image_urls),
        "tts": _nonempty(getattr(task, "audio_url", None)),
        "video": _nonempty(getattr(task, "video_url", None)),
        "subtitle": _nonempty(getattr(task, "subtitle_url", None)),
    }

# 各阶段并发数取自系统设置（不同生成类型不同并发）
def _stage_concurrency(stage: str) -> int:
    mapping = {
        "script": settings.text_concurrency,
        "character": settings.image_concurrency,   # 定妆照走图片接口，共用图片并发桶
        "image": settings.image_concurrency,
        "video": settings.video_concurrency,        # 默认 1（agnes 视频 1 次/分钟）
        "tts": settings.tts_concurrency,
        "subtitle": settings.subtitle_concurrency,
    }
    return max(1, mapping.get(stage, 1))


class DynamicSemaphore:
    """并发闸门（配置热更版）。

    与 asyncio.Semaphore 的差异：每次 ``locked()``/``acquire()`` 均**实时**
    调用 ``getter`` 读取当前目标并发数——设置页改了 text/image/video 并发后
    不重启即生效（旧版 Semaphore 在启动时把数值固化，改配置无效）。

    接口面与队列现有用法对齐：``locked()`` / ``await acquire()`` / ``release()``。
    等待者在被唤醒时按**当时**的配置值重新竞争，配置调大即自动多放行。
    """

    def __init__(self, getter):
        self._getter = getter                # () -> int，实时读取配置
        self._current = 0                    # 当前持有数
        self._waiters: deque[asyncio.Event] = deque()
        self._lock = asyncio.Lock()

    def locked(self) -> bool:
        return self._current >= self._getter()

    async def acquire(self) -> None:
        while True:
            ev: asyncio.Event | None = None
            async with self._lock:
                if self._current < self._getter():
                    self._current += 1
                    return
                ev = asyncio.Event()
                self._waiters.append(ev)
            await ev.wait()
            # 被唤醒后回循环按最新配置重新竞争；未抢到则重新排队

    def release(self) -> None:
        self._current = max(0, self._current - 1)
        # 唤醒全部等待者重新竞争（等待者数量 = 队列深度，量级极小）
        while self._waiters:
            ev = self._waiters.popleft()
            ev.set()


class QueueService:
    """生成队列（单进程内消费者；DB 持久化，重启安全）"""

    def __init__(self):
        self._sems: dict[str, DynamicSemaphore] = {
            stage: DynamicSemaphore(lambda stage=stage: _stage_concurrency(stage))
            for stage in STAGE_ORDER
        }
        self._stop = False
        self._task: Optional[asyncio.Task] = None
        self._poll_interval = settings.queue_poll_interval
        self._leader = False  # 是否持有队列消费权（单实例锁，见 _acquire_instance_lock）

    # ------------------------------------------------------------------ #
    # 生产者：入队一个任务的所有阶段
    # ------------------------------------------------------------------ #
    async def enqueue_task(
        self,
        task_id: int,
        stages: Optional[list[str]] = None,
        clear_outputs: bool = False,
        source: str = "unknown",
    ) -> int:
        """为任务创建阶段 Job（pending）。

        Args:
            task_id: 业务任务 ID
            stages: 要生成的阶段；默认全部
            clear_outputs: 是否清空该任务已有的视觉产物（用于重新生成）
            source: 调用来源标识（create_task / regenerate / batch / recover ...），
                    仅用于审计日志——2026-09-09 事故：任务"莫名又重新生成了一遍"却
                    无法从 DB 反查是谁触发的。所有调用方必须自报来源。
        Returns:
            创建的 Job 数量
        """
        stages = stages or list(STAGE_ORDER)

        async with async_session_factory() as session:
            # 清理该任务旧 Job：只删 pending/running（未产生历史、留着会重复消费），
            # 保留 done/failed 终态 Job 作为执行历史 —— 重新生成时进度弹窗的
            # 尝试记录是「追加」而不是清零（2026-09-09 用户定夺的语义）。
            old = (await session.execute(select(Job).where(Job.task_id == task_id))).scalars().all()
            inflight = [j for j in old if j.status in ("pending", "running")]
            if inflight:
                # 审计：在途 Job 被覆盖 = 已在跑的分镜/视频会被丢弃重来（产物同时被
                # clear_outputs 清空）。这类"用户没点却重新生成"的投诉曾无法定位，
                # 这里把覆盖明细与来源打进 WARNING，便于事后追责。
                logger.warning(
                    "⚠️ 入队覆盖在途 Job task=%s source=%s stages=%s clear_outputs=%s "
                    "被覆盖=[%s]",
                    task_id, source, list(stages), clear_outputs,
                    ",".join(f"#{j.id}:{j.stage}:{j.status}" for j in inflight),
                )
            for j in old:
                if j.status in ("pending", "running"):
                    await session.delete(j)

            # 先取 task 并在「清空产物之前」计算已产出项，
            # 再据此做依赖补全（否则 character_ref 被清掉后会误判为前置缺失）
            task = await session.get(Task, task_id)
            produced = _task_produced(task) if task else {}
            expanded = _expand_prereqs(stages, produced)
            if expanded != list(stages):
                logger.info(f"入队任务 {task_id} 依赖补全: {list(stages)} -> {expanded}")
            stages = expanded

            if clear_outputs and task is not None:
                # 只清空本次要重跑的阶段自己的产物字段，其余阶段产物保留
                # （例：regenerate?stage=image 保留 script / character_ref）
                cleared: list[str] = []
                for st in stages:
                    for field in STAGE_OUTPUTS.get(st, []):
                        if getattr(task, field, None) is not None:
                            setattr(task, field, None)
                            cleared.append(field)
                task.status = "processing"
                task.error_message = None
                task.review_status = "pending"
                task.completed_at = None
                if len(stages) >= len(STAGE_ORDER):
                    task.progress = 0
                await session.commit()
                if cleared:
                    logger.info(f"入队任务 {task_id} 清空产物字段: {','.join(cleared)}")

            count = 0
            for stage in stages:
                job = Job(
                    task_id=task_id,
                    stage=stage,
                    status="pending",
                    priority=STAGE_PRIORITY.get(stage, 0),
                    attempts=0,
                )
                session.add(job)
                count += 1
            await session.commit()

        logger.info(
            f"入队任务 {task_id}: {count} 个阶段 ({','.join(stages)}) "
            f"source={source} clear_outputs={clear_outputs}"
        )
        return count

    # ------------------------------------------------------------------ #
    # 重启恢复：把孤儿 running Job 复位为 pending
    # ------------------------------------------------------------------ #
    async def recover(self):
        """后端启动时调用：将上次运行残留的 running Job 复位为 pending，使其被重新消费。"""
        async with async_session_factory() as session:
            res = await session.execute(select(Job).where(Job.status == "running"))
            orphan = res.scalars().all()
            for j in orphan:
                j.status = "pending"
                j.started_at = None
                j.last_error = (j.last_error or "") + " [restart-recover reset]"
                j.attempts = max(0, j.attempts - 1)  # 不计算崩溃那次为失败
            if orphan:
                await session.commit()
                logger.warning(f"队列恢复：复位 {len(orphan)} 个孤儿 running Job 为 pending")
            else:
                logger.info("队列恢复：无孤儿 Job")

        # 孤儿任务自愈（2026-09-09）：旧版 create_task 用 run_pipeline 后台直跑、
        # 不产生任何 Job，后端重启后这些 processing 任务无人推进也无从恢复。
        # 补建全套阶段 Job——run_stage 对已完成阶段按产物自检（_have）跳过，幂等。
        async with async_session_factory() as session:
            res = await session.execute(select(Task.id).where(Task.status == "processing"))
            processing_ids = [r[0] for r in res.all()]
        heal_ids = []
        for tid in processing_ids:
            async with async_session_factory() as session:
                has_job = (await session.execute(
                    select(Job.id).where(Job.task_id == tid).limit(1)
                )).scalar()
            if has_job is None:
                heal_ids.append(tid)
        if heal_ids:
            logger.warning(
                f"队列恢复：{len(heal_ids)} 个 processing 任务无任何 Job"
                f"（旧直跑路径遗留），补建全套阶段: {heal_ids}"
            )
            for tid in heal_ids:
                await self.enqueue_task(task_id=tid, source="recover")

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def start(self):
        """启动消费者循环（在 FastAPI lifespan 中调用）

        单实例约束（2026-09-10 并发加固 P0-B）：拿不到进程级锁时不启动消费者，
        仅作为 API 进程存在。这样 --reload 重启窗口 / 误起第二实例 / 测试触发
        lifespan 都不会出现两个消费者同时扫 pending 的情况。
        """
        if self._task is not None:
            return
        if not _acquire_instance_lock():
            self._leader = False
            logger.warning(
                "⚠️ 未取得队列单实例锁：本进程只提供 API（可入队/查询），不消费队列"
            )
            return
        self._leader = True
        await self.recover()
        self._stop = False
        self._task = asyncio.create_task(self._worker_loop())
        logger.info("生成队列消费者已启动（本进程 pid=%s 持有消费权）", os.getpid())

    async def stop(self):
        """停止消费者循环"""
        self._stop = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._leader:
            _release_instance_lock()
            self._leader = False
        logger.info("生成队列消费者已停止")

    # ------------------------------------------------------------------ #
    # 消费者主循环
    # ------------------------------------------------------------------ #
    async def _worker_loop(self):
        while not self._stop:
            try:
                _touch_instance_lock()  # 心跳：僵死超过阈值后锁可被其他实例回收
                claimed = await self._claim_and_dispatch()
            except Exception as e:
                logger.error(f"队列消费者异常: {e}")
                claimed = 0
            if claimed == 0:
                await asyncio.sleep(self._poll_interval)

    async def _claim_and_dispatch(self) -> int:
        """扫描待办 Job，认领一个可执行的（依赖满足 + 阶段并发未满），派发执行。

        严格任务间串行（一个任务必须整体跑完才轮到下一个）：
        - 有 running Job → 只推进该任务的后续阶段，绝不跨任务并发；
        - 无 running Job → 在所有有 pending Job 的任务里，按 task_id 升序
          取第一个（先创建的先执行）；失败的任务排到最后。
        同任务内不同阶段仍受各阶段并发桶约束（text/image 可并行，video 串行）。
        """
        async with async_session_factory() as session:
            # 有 running Job → 锁定该任务（串行约束：同一时刻只有一个任务在跑）
            running_task = (await session.execute(
                select(Job.task_id).where(Job.status == "running").order_by(Job.task_id.asc()).limit(1)
            )).scalar()

            if running_task is not None:
                active_task_id = running_task
            else:
                # 无 running → 取 task_id 最小的有 pending Job 的任务
                pending_task_id = (await session.execute(
                    select(Job.task_id).where(Job.status == "pending").order_by(Job.task_id.asc()).limit(1)
                )).scalar()
                if pending_task_id is None:
                    return 0  # 没有 pending Job 了
                active_task_id = pending_task_id

            # 孤儿防御（2026-09-09 事故）：活动任务的 Task 记录已被删除（如用户
            # 删任务后子表 Job 未级联清理的存量数据），其 pending Job 依赖检查
            # 永远失败但永远占着"活动任务"位置（按优先级它总排第一），把其他
            # 任务全部饿死。这里直接清掉孤儿 pending Job，下一轮自然轮到别的任务。
            if await session.get(Task, active_task_id) is None:
                orphan = (await session.execute(
                    select(Job).where(
                        Job.task_id == active_task_id, Job.status == "pending"
                    )
                )).scalars().all()
                if orphan:
                    for j in orphan:
                        await session.delete(j)
                    await session.commit()
                    logger.warning(
                        "🧹 孤儿清理：task=%s 已不存在，删除 %s 个 pending Job"
                        "（防饿死其他任务）",
                        active_task_id, len(orphan),
                    )
                return 0

            # 重新拉取 pending Job（用 active_task_id 过滤）
            res = await session.execute(
                select(Job)
                .where(Job.status == "pending", Job.task_id == active_task_id)
                .order_by(Job.priority.desc(), Job.created_at.asc(), Job.id.asc())
            )
            candidates = res.scalars().all()
            if not candidates:
                return 0

            for job in candidates:
                sem = self._sems[job.stage]
                if sem.locked():
                    continue
                if not await self._deps_satisfied(session, job):
                    await self._heal_prereqs(session, job)
                    continue

                now = datetime.now(timezone.utc)
                claimed = await session.execute(
                    update(Job)
                    .where(Job.id == job.id, Job.status == "pending")
                    .values(status="running", started_at=now)
                )
                await session.commit()
                if claimed.rowcount != 1:
                    logger.warning(
                        "⏭️ Job#%s task=%s stage=%s 已被其他消费者认领，本轮跳过",
                        job.id, job.task_id, job.stage,
                    )
                    continue
                job.status = "running"
                job.started_at = now

                await sem.acquire()
                asyncio.create_task(self._run_job(job.id, sem))
                return 1
        return 0

    async def _heal_prereqs(self, session: AsyncSession, job: Job) -> None:
        """自愈：为「依赖未满足且无在途 Job」的前置阶段补建 Job。

        触发场景（真实发生过）：task 9 的 script Job 被标记为 done，但
        ``task.script`` 实际为空（跳过路径误标完成 / 产物后来被清空），
        导致 character / image / tts / video / subtitle 五个 Job 全部
        永久阻塞在 pending。仅靠入队时的 ``_expand_prereqs`` 救不了已经
        入队的存量 Job，所以消费者侧需要自愈。

        防死循环：已经 **failed** 的前置阶段不补建，改为级联失败本任务
        全部 pending Job（见 ``_cascade_fail_pending``），让任务干净落终态。
        """
        prereqs = STAGE_PREREQS.get(job.stage, set())
        if not prereqs:
            return
        task = await session.get(Task, job.task_id)
        if task is None:
            return
        produced = _task_produced(task)
        missing = [p for p in STAGE_ORDER if p in prereqs and not produced.get(p, False)]
        if not missing:
            return

        res = await session.execute(
            select(Job.stage, Job.status).where(Job.task_id == job.task_id)
        )
        rows = res.all()
        active = {s for s, st in rows if st in ("pending", "running")}
        failed = {s for s, st in rows if st == "failed"}

        created: list[str] = []
        for st in missing:
            if st in active:
                continue  # 已在途，等它跑完
            if st in failed:
                # 前置已永久失败 → 任务注定无法推进。级联失败本任务全部
                # pending Job（2026-09-09 用户定夺：UI 已显示失败，任务直接挂掉，
                # 不留僵尸 Job 每 2s 刷一条"不自动补建"警告）。
                # 重生成时 enqueue_task 会清掉旧 Job 重建，因此级联失败可安全覆盖。
                await self._cascade_fail_pending(session, job.task_id, reason=f"前置阶段 {st} 永久失败")
                return
            session.add(
                Job(
                    task_id=job.task_id,
                    stage=st,
                    status="pending",
                    priority=STAGE_PRIORITY.get(st, 0),
                    attempts=0,
                )
            )
            created.append(st)
        if created:
            await session.commit()
            logger.warning(
                f"🩹 自愈入队 task={job.task_id}: 补建缺失前置 {created}"
                f"（由 {job.stage} 触发）"
            )

    async def _cascade_fail_pending(self, session: AsyncSession, task_id: int, reason: str) -> None:
        """级联失败：把任务全部 pending Job 置 failed 并落任务终态。

        触发条件：任一前置阶段已永久失败（重试耗尽），任务不可能推进。
        幂等：没有 pending Job 时什么都不做（不会反复刷日志）。
        """
        res = await session.execute(
            select(Job).where(Job.task_id == task_id, Job.status == "pending")
        )
        zombies = res.scalars().all()
        if not zombies:
            return
        for j in zombies:
            j.status = "failed"
            j.last_error = f"[级联失败] {reason}"
            j.finished_at = datetime.now(timezone.utc)
        await session.commit()
        logger.warning(
            f"⚰ task={task_id} 前置永久失败，级联失败 {len(zombies)} 个 pending Job: {reason}"
        )
        await self._maybe_finalize(session, task_id)

    async def _deps_satisfied(self, session: AsyncSession, job: Job) -> bool:
        """依赖是否全部满足：检查 task 实际产物是否存在。

        v2 修复（D5）：旧版查同任务的 Job.status==done，导致
        ``regenerate?stage=image`` 只入队 image 这一条 Job 时，前置
        ``script/character`` 因为没有对应 Job → 永久判定失败（死锁）。
        改为查 task 实际产物：script 字段（短文本）/ image_urls（JSON 数组）
        / audio_url / video_url。这样已经生成过的产物自然满足依赖，
        实现"幂等重新生成某个阶段"的需求。
        """
        prereqs = STAGE_PREREQS.get(job.stage, set())
        if not prereqs:
            return True
        task = await session.get(Task, job.task_id)
        if task is None:
            return False
        # 产物表：阶段名 -> bool(是否已生成该阶段产物)
        produced = _task_produced(task)
        if not all(produced.get(p, False) for p in prereqs):
            return False
        # 在途阻断（2026-09-09 事故）：产物可能在阶段中途落库（script 文本在
        # _generate_script 内先提交，分镜随后才生成），产物存在 ≠ 生产者收工。
        # 前置阶段仍有 pending/running Job 时不得放行，否则下游与前置并发——
        # 真实事故：tts 在 script 生成分镜前抢跑，读到空 storyboard 三连失败，
        # 并级联拖死 image/video/subtitle，全任务报废。
        active = (await session.execute(
            select(Job.stage).where(
                Job.task_id == job.task_id,
                Job.status.in_(("pending", "running")),
                Job.stage.in_(prereqs),
            )
        )).scalars().all()
        return not active

    # ------------------------------------------------------------------ #
    # 单 Job 执行
    # ------------------------------------------------------------------ #
    async def _run_job(self, job_id: int, sem: asyncio.Semaphore):
        try:
            # 视频阶段：进入前先按 agnes 1次/分钟 规约等待（D6/D7 修复）。
            # 旧版仅靠 sem=1 限制"同时 1 个"，但两条 video 任务间隔 30s
            # 仍会被 agnes 拒为 rate_limit_exceeded。这里加全局最小间隔：
            # 上一次提交到现在 >= 60s 才放行，否则 sleep 余下的秒数。
            # 间隔可通过 settings.video_min_interval 调整。
            job = await _load_job(job_id)
            if job and job.stage == "video":
                await self._await_video_slot(job.task_id, job_id)

            async with async_session_factory() as session:
                job = await session.get(Job, job_id)
                if not job:
                    return
                task_id = job.task_id
                stage = job.stage
                logger.info(f"▶ 执行 Job#{job_id} task={task_id} stage={stage}")

                try:
                    await pipeline_engine.run_stage(session, task_id, stage)
                    job.status = "done"
                    job.finished_at = datetime.now(timezone.utc)
                    self._log_attempt(job, ok=True)
                    if stage == "video":
                        # 记录提交时间戳：下一个 video Job 至少等待这个时刻 + min_interval
                        _mark_video_submitted()
                    await session.commit()
                    logger.info(f"✅ Job#{job_id} task={task_id} stage={stage} 完成")
                except Exception as e:
                    job.attempts += 1
                    job.last_error = str(e)[:800]
                    self._log_attempt(job, ok=False, error=str(e)[:300])
                    # 未超重试次数 → 退回 pending 自动重试；否则标记 failed
                    if job.attempts < max(1, settings.max_retries):
                        job.status = "pending"
                        logger.warning(
                            f"⚠️ Job#{job_id} task={task_id} stage={stage} 失败"
                            f"（第 {job.attempts} 次，将重试）: {e}"
                        )
                    else:
                        job.status = "failed"
                        logger.error(
                            f"❌ Job#{job_id} task={task_id} stage={stage} 永久失败: {e}"
                        )
                    job.finished_at = datetime.now(timezone.utc)
                    await session.commit()

                # 阶段结束后，检查是否可进入终态
                await self._maybe_finalize(session, task_id)
        finally:
            sem.release()

    @staticmethod
    def _log_attempt(job: Job, ok: bool, error: str = "") -> None:
        """往 Job.attempts_log 追加一条尝试记录（JSON 数组，供进度/日志视图）。

        存储格式：[{attempt, at, ok, error}]；任务列表弹窗按此渲染
        "第 N 次尝试 失败/成功 + 原因"。max_retries=3 → 最多 4 条（3 失败 + 1 成功）。
        """
        import json as _json
        try:
            log = _json.loads(job.attempts_log) if job.attempts_log else []
        except (ValueError, TypeError):
            log = []
        log.append({
            "attempt": (job.attempts or 0) + (1 if ok else 0),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ok": ok,
            "error": None if ok else (error or "未知错误"),
        })
        job.attempts_log = _json.dumps(log, ensure_ascii=False)

    async def _await_video_slot(self, task_id: int, job_id: int):
        """距上次 video 提交未到 min_interval 秒则阻塞等待（主动节流）。"""
        interval = max(60, getattr(settings, "video_min_interval", 62))
        while True:
            async with _video_state_lock:
                last = _last_video_submitted_at
                if last is None:
                    break
                elapsed = (datetime.now(timezone.utc) - last).total_seconds()
                if elapsed >= interval:
                    break
                wait = interval - elapsed
            logger.info(
                f"⏳ video Job#{job_id} task={task_id} 等待 {wait:.1f}s "
                f"(距上次 video 提交 {elapsed:.1f}s, 间隔阈值 {interval}s)"
            )
            await asyncio.sleep(min(wait + 0.5, interval + 1))
            # 退出 while 由再次进入临界区判断

    async def _maybe_finalize(self, session: AsyncSession, task_id: int):
        """若任务所有阶段均 done → 置 pending_review；若有 failed → 置 failed。

        终态判定只看「每阶段最新一条 Job」（按 id 升序，后者覆盖）。2026-09-09
        任务001事故：重新生成保留历史 Job 后，旧一轮的 subtitle 级联失败记录
        把 image/video 刚跑完的任务拖成 failed。同理，存在 pending/running
        Job 时任务仍在推进，不做终态判定（否则刚失败完又重试的场景被误判）。
        """
        res = await session.execute(
            select(Job).where(Job.task_id == task_id).order_by(Job.id.asc())
        )
        jobs = res.scalars().all()
        if not jobs:
            return
        if any(j.status in ("pending", "running") for j in jobs):
            return  # 仍在推进（或新一轮重试在途），不是终态
        latest: dict[str, str] = {}
        for j in jobs:
            latest[j.stage] = j.status  # id 升序遍历 → 每阶段留最新状态
        states = latest
        task = await session.get(Task, task_id)
        if not task:
            return

        if any(s == "failed" for s in states.values()):
            if task.status != "failed":
                task.status = "failed"
                task.error_message = "生成阶段失败（详见 generation_jobs）"
                await session.commit()
            return

        if all(s == "done" for s in states.values()):
            if task.status not in ("pending_review", "done"):
                task.status = "pending_review"
                task.current_stage = "subtitle"
                task.progress = 95
                task.review_status = "pending"
                task.completed_at = datetime.now(timezone.utc)
                await session.commit()
                logger.info(f"🎉 任务 {task_id} 全部阶段完成 → pending_review")

    # ------------------------------------------------------------------ #
    # 状态查询（供 API / 调试）
    # ------------------------------------------------------------------ #
    async def status(self) -> dict:
        async with async_session_factory() as session:
            res = await session.execute(select(Job))
            jobs = res.scalars().all()
            by_status = {}
            by_task = {}
            for j in jobs:
                by_status[j.status] = by_status.get(j.status, 0) + 1
                by_task.setdefault(j.task_id, []).append(
                    {"stage": j.stage, "status": j.status, "priority": j.priority,
                     "attempts": j.attempts, "error": (j.last_error or "")[:120]}
                )
            return {
                "poll_interval": self._poll_interval,
                "concurrency": {s: _stage_concurrency(s) for s in STAGE_ORDER},
                "counts_by_status": by_status,
                "tasks": by_task,
            }


# 全局队列实例
queue_service = QueueService()
