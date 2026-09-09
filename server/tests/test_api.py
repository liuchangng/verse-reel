"""API 单元测试（test-health-20260908：恢复进全量，摆脱外部 Agnes 依赖）。

- 全部 /api 测试挂鉴权 fixture（REQ-S2 落地后 /api 需 Bearer Token）
- test_create_task mock `pipeline_engine.run_pipeline` 为 no-op：
  BackgroundTasks 在 TestClient 响应后同步执行，真实链路会调外部 AI 服务
  （曾实测 6m41s 未完成）。本测试只验证 API 契约（创建成功 + processing），
  流水线执行由 pipeline/queue 各自测试承担。
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app

# enable_auth / auth_headers 来自 tests/conftest.py（REQ-2 去重）


@pytest.fixture
def client(enable_auth, monkeypatch):
    """创建测试客户端（with 上下文以触发 lifespan → init_db 建表）。

    - 默认携带 Bearer header（/api 已挂 require_token，裸请求会 401）
    - mock queue_service.start：lifespan 会恢复真实 DB 中的遗留 Job 并执行流水线
      （实测日志出现「▶ 执行 Job#1 task=1 stage=script」），API 契约测试不需要真队列
    """
    from app.services.queue import queue_service

    async def _noop_start():
        return None

    monkeypatch.setattr(queue_service, "start", _noop_start)
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {enable_auth}"})
        yield c


class TestRootAPI:
    """根路径 API 测试"""

    def test_root(self, client):
        """测试根路径"""
        response = client.get('/')
        assert response.status_code == 200
        data = response.json()
        assert 'name' in data
        assert 'version' in data
        assert data['status'] == 'running'

    def test_health(self, client):
        """测试健康检查"""
        response = client.get('/health')
        assert response.status_code == 200
        assert response.json()['status'] == 'healthy'


class TestPoemsAPI:
    """诗词 API 测试"""

    def test_list_poems(self, client):
        """测试诗词列表"""
        response = client.get('/api/poems/')
        assert response.status_code == 200
        data = response.json()
        assert 'items' in data
        assert 'total' in data
        assert 'page' in data

    def test_poem_stats(self, client):
        """测试诗词统计"""
        response = client.get('/api/poems/stats')
        assert response.status_code == 200
        data = response.json()
        assert 'total' in data
        assert 'dynasty_distribution' in data
        assert 'genre_distribution' in data


class TestTasksAPI:
    """任务 API 测试"""

    def test_list_tasks(self, client):
        """测试任务列表"""
        response = client.get('/api/tasks/')
        assert response.status_code == 200
        data = response.json()
        assert 'items' in data
        assert 'total' in data

    def test_create_task(self, client, monkeypatch):
        """测试创建任务（mock 流水线执行，不依赖外部 AI 服务）"""
        # mock BackgroundTasks 链路终点：run_pipeline no-op，避免真实外部调用
        async def _noop_pipeline(session, task_id):
            return None

        from app.api import tasks as tasks_mod
        monkeypatch.setattr(tasks_mod.pipeline_engine, "run_pipeline", _noop_pipeline)

        # 先获取一个诗词 ID
        poems_response = client.get('/api/poems/')
        poems = poems_response.json()['items']

        if poems:
            poem_id = poems[0]['id']
            response = client.post(f'/api/tasks?poem_id={poem_id}')
            assert response.status_code == 200
            data = response.json()
            assert 'id' in data
            # 2026-09-09 统一生命周期：创建即入队（generation_jobs 全套阶段），
            # 任务状态保持 'pending' 由队列推进；进度可在任务详情看各阶段执行记录
            assert data['status'] == 'pending'
