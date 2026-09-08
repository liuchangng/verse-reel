"""WebSocket 进度推送"""
import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, Set
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select, or_

from app.database import async_session_factory
from app.models.task import Task

logger = logging.getLogger(__name__)
router = APIRouter()


class ConnectionManager:
    """WebSocket 连接管理器"""
    
    def __init__(self):
        # task_id -> Set[WebSocket]
        self.active_connections: Dict[int, Set[WebSocket]] = {}
    
    async def connect(self, websocket: WebSocket, task_id: int):
        """接受连接"""
        await websocket.accept()
        
        if task_id not in self.active_connections:
            self.active_connections[task_id] = set()
        self.active_connections[task_id].add(websocket)
        logger.info(f"WebSocket 客户端连接: task_id={task_id}")
    
    def disconnect(self, websocket: WebSocket, task_id: int):
        """断开连接"""
        if task_id in self.active_connections:
            self.active_connections[task_id].discard(websocket)
            if not self.active_connections[task_id]:
                del self.active_connections[task_id]
        logger.info(f"WebSocket 客户端断开: task_id={task_id}")
    
    async def send_progress(self, task_id: int, data: dict):
        """向指定任务的所有客户端发送进度"""
        if task_id in self.active_connections:
            disconnected = set()
            for connection in self.active_connections[task_id]:
                try:
                    await connection.send_json(data)
                except Exception:
                    disconnected.add(connection)
            
            # 清理断开的连接
            for conn in disconnected:
                self.active_connections[task_id].discard(conn)


# 全局连接管理器
manager = ConnectionManager()


def _ws_authorized(websocket: WebSocket) -> bool:
    """WS token 校验（安全加固 REQ-S2；review MINOR-1 常量时间比较）。

    query 参数 token 须等于 settings.app_token；
    app_token 未配置（空）时一律拒绝（fail-closed）。
    """
    import secrets

    from app.config import settings
    if not settings.app_token:
        return False
    token = websocket.query_params.get("token", "")
    return bool(token) and secrets.compare_digest(token, settings.app_token)


@router.websocket("/progress/{task_id}")
async def websocket_progress(websocket: WebSocket, task_id: int):
    """
    任务进度实时推送
    
    客户端连接后，服务器会定时推送任务进度更新
    """
    if not _ws_authorized(websocket):
        logger.warning("WS /progress/%s 拒绝：token 缺失或不匹配", task_id)
        await websocket.close(code=4401)
        return

    await manager.connect(websocket, task_id)
    
    try:
        while True:
            # 检查是否有来自客户端的消息（保持连接）
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                # 客户端可以发送 ping
                if data == "ping":
                    await websocket.send_json({"type": "pong"})
            except asyncio.TimeoutError:
                pass
            
            # 查询最新进度
            async with async_session_factory() as session:
                task = await session.get(Task, task_id)
                if task:
                    # 解析 image_urls
                    img_urls = None
                    if task.image_urls:
                        try: img_urls = json.loads(task.image_urls)
                        except Exception as exc:
                            logger.warning("任务 %s image_urls 解析失败: %s", task_id, exc)

                    await manager.send_progress(task_id, {
                        "type": "progress",
                        "task_id": task_id,
                        "status": task.status,
                        "current_stage": task.current_stage,
                        "progress": task.progress,
                        "script_score": task.script_score,
                        "image_score": task.image_score,
                        "image_urls": img_urls,
                        "video_url": task.video_url,
                        "video_duration": task.video_duration,
                    })
            
            # 每秒推送一次
            await asyncio.sleep(1)
            
    except WebSocketDisconnect:
        manager.disconnect(websocket, task_id)
    except Exception as e:
        logger.error(f"WebSocket 错误: {e}")
        manager.disconnect(websocket, task_id)


@router.websocket("/tasks")
async def websocket_tasks(websocket: WebSocket):
    """
    任务列表实时更新
    
    所有任务的状态变化都会推送给连接的客户端
    """
    if not _ws_authorized(websocket):
        logger.warning("WS /tasks 拒绝：token 缺失或不匹配")
        await websocket.close(code=4401)
        return

    await websocket.accept()
    
    # 增量推送游标（REQ-S4.3）：首轮全量，之后只推 updated_at 之后 / 新增 id 的变更，
    # 避免每 2 秒全表扫描 + 全量下发
    last_seen: datetime | None = None
    last_max_id: int = 0

    try:
        while True:
            # 查询（增量条件）任务状态
            async with async_session_factory() as session:
                query = select(Task)
                if last_seen is not None:
                    query = query.where(or_(Task.updated_at > last_seen, Task.id > last_max_id))
                result = await session.execute(query)
                tasks = result.scalars().all()
                if tasks:
                    await websocket.send_json({
                        "type": "tasks_update",
                        "tasks": [
                            {
                                "id": task.id,
                                "status": task.status,
                                "current_stage": task.current_stage,
                                "progress": task.progress,
                            }
                            for task in tasks
                        ]
                    })
                    # 推进游标：id 取本轮最大；时间取本轮最新 updated_at（无则保留现值）
                    last_max_id = max(last_max_id, max(t.id for t in tasks))
                    ts = [t.updated_at for t in tasks if t.updated_at]
                    if ts:
                        last_seen = max(ts)
                    if last_seen is None:
                        last_seen = datetime.now()
            
            # 每2秒推送一次
            await asyncio.sleep(2)
            
    except WebSocketDisconnect:
        logger.info("任务列表客户端断开")
    except Exception as e:
        logger.error(f"WebSocket 错误: {e}")
