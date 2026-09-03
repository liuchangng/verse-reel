"""API 单元测试"""
import pytest
from fastapi.testclient import TestClient
from app.main import app


@pytest.fixture
def client():
    """创建测试客户端（with 上下文以触发 lifespan → init_db 建表）"""
    with TestClient(app) as c:
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
    
    def test_create_task(self, client):
        """测试创建任务"""
        # 先获取一个诗词 ID
        poems_response = client.get('/api/poems/')
        poems = poems_response.json()['items']
        
        if poems:
            poem_id = poems[0]['id']
            response = client.post(f'/api/tasks?poem_id={poem_id}')
            assert response.status_code == 200
            data = response.json()
            assert 'id' in data
            # create_task 会立即后台启动流水线，故返回 'processing'
            # （流水线真正执行依赖外部 AI 服务，网络不可达时后台失败但不影响创建响应）
            assert data['status'] == 'processing'
