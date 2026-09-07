"""WebSocket 进度推送"""
import asyncio
import json
import logging
from typing import Dict, Set
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

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
    """WS token 校验（安全加固 REQ-S2）：query 参数 token 须等于 settings.app_token。

    app_token 未配置（空）时一律拒绝（fail-closed）。
    """
    from app.config import settings
    if not settings.app_token:
        return False
    return websocket.query_params.get("token", "") == settings.app_token


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
                        except: pass

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
    
    try:
        while True:
            # 查询所有任务状态
            async with async_session_factory() as session:
                result = await session.execute(select(Task))
                tasks = result.scalars().all()
                
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
            
            # 每2秒推送一次
            await asyncio.sleep(2)
            
    except WebSocketDisconnect:
        logger.info("任务列表客户端断开")
    except Exception as e:
        logger.error(f"WebSocket 错误: {e}")
