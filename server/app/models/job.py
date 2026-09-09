"""生成任务队列 Job 模型 - 生产者/消费者模式，DB 持久化，重启可恢复

每个 Job 代表一个任务(task)的一个生成阶段(stage)：
  script/character/image/video/tts/subtitle

设计目标（见需求讨论）：
- 文本/图片生成很快，视频很慢（agnes 视频硬限流 1 次/分钟）。
- 方式二：把文本+图片按批先生成（跨任务并发），视频按任务依次串行生成。
- 用数据库做队列，本质是生产者-消费者；后端重启后能继续执行未完成的 Job。
"""
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Index
from sqlalchemy.sql import func
from app.database import Base


class Job(Base):
    """生成阶段任务（队列单元）"""

    __tablename__ = "generation_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 关联的业务任务（诗词视频任务）
    task_id = Column(
        Integer, ForeignKey("tasks.id"), nullable=False, index=True,
        comment="关联 tasks.id",
    )
    # 阶段：script/character/image/video/tts/subtitle
    stage = Column(String(20), nullable=False, comment="生成阶段")
    # 状态：pending/running/done/failed
    status = Column(
        String(20), default="pending", nullable=False, index=True,
        comment="pending/running/done/failed",
    )
    # 优先级：数值越大越先被消费（快速阶段高、视频低，实现方式二批处理）
    priority = Column(Integer, default=0, comment="消费优先级（越大越先）")
    # 额外参数（JSON 字符串）
    payload = Column(Text, nullable=True, comment="阶段额外参数(JSON)")
    # 重试次数
    attempts = Column(Integer, default=0, comment="已尝试次数")
    # 每次尝试的历史（JSON 数组：[{attempt, at, ok, error}]，供任务列表"进度/日志"展示；
    # max_retries=3 时最多 3 条失败记录 + 1 条成功记录）
    attempts_log = Column(Text, nullable=True, comment="尝试历史(JSON)")
    # 最近一次错误
    last_error = Column(Text, nullable=True, comment="最近错误")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    started_at = Column(DateTime(timezone=True), nullable=True, comment="开始执行时间")
    finished_at = Column(DateTime(timezone=True), nullable=True, comment="结束时间")

    def __repr__(self):
        return f"<Job(id={self.id}, task_id={self.task_id}, stage='{self.stage}', status='{self.status}')>"


# 复合索引：消费者按 (status, priority) 扫描待办
Index("ix_jobs_status_priority", Job.status, Job.priority)
