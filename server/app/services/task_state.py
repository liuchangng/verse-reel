"""task 行原子更新工具（2026-09-10 并发加固 P1-D）

为什么需要：``tasks`` 行的状态字段（status / current_stage / progress /
review_status）有多个写者——pipeline 各阶段收尾、队列终态判定 ``_maybe_finalize``、
REST API（审核 / 重新生成）。ORM 的「get → 改属性 → commit」在两个写者交错时会
互相覆盖（lost update）。

典型事故形态：用户点"审核通过"（写 done）与队列终态写入（写 pending_review）
交错，且后者读到的 status 是改写前的旧值 → 用户的操作被静默回滚，前端表现为
"刚刚通过了又变回待审核"。

本模块提供**列级原子 UPDATE**：只更新指定列，并支持 ``only_if`` 乐观条件
（WHERE 当前值 = 期望值）。读-改-写之间由数据库保证不被插队，条件不满足时不写入，
由调用方决定重试还是报错。
"""
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import Task


async def patch_task(
    session: AsyncSession,
    task_id: int,
    *,
    only_if: dict | None = None,
    **fields,
) -> bool:
    """原子更新 tasks 单行的指定字段。

    Args:
        session: 数据库会话（本函数内部会 commit）
        task_id: 任务 ID
        only_if: 乐观并发条件 ``{列名: 期望当前值}``；任一不满足则不写入。
            例：``only_if={"status": "pending_review"}`` 保证只有待审核态能迁移。
        **fields: 要写入的字段（列级，不触碰其他列）

    Returns:
        True = 更新命中（rowcount == 1）；False = 条件不满足或任务不存在。
    """
    if not fields:
        return False

    stmt = update(Task).where(Task.id == task_id).values(**fields)
    for col, expected in (only_if or {}).items():
        stmt = stmt.where(getattr(Task, col) == expected)

    res = await session.execute(stmt)
    await session.commit()
    return res.rowcount == 1
