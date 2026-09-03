"""系统设置持久化模型（单行 KV 表）"""
from sqlalchemy import Column, Integer, String, Text, DateTime, func
from app.database import Base


class SystemSetting(Base):
    """系统全局设置（单行存储，以 JSON 形式持久化配置）"""
    __tablename__ = "system_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 当前固定为 "singleton"，避免未来引入多租户时迁移数据
    scope = Column(String(32), unique=True, nullable=False, default="singleton")
    # 配置 JSON 字符串（与 Pydantic Settings 同名字段一一对应）
    config_json = Column(Text, nullable=False, default="{}")
    # 元数据
    version = Column(Integer, default=1, comment="配置版本号")
    updated_at = Column(DateTime, server_default=func.current_timestamp(),
                        onupdate=func.current_timestamp(), nullable=False)
