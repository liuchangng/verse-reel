# DEPLOY.md — 部署记录

> 部署时间：2026-08-30
> 部署版本：v0.1.0
> 部署环境：本地开发环境

---

## 部署信息

| 项目 | 值 |
|---|---|
| 后端地址 | http://localhost:8000 |
| 前端地址 | http://localhost:5173 |
| API 文档 | http://localhost:8000/docs |
| 健康检查 | http://localhost:8000/health |

---

## 服务状态

```
✅ 后端服务: 运行中
✅ 前端服务: 运行中
✅ 数据库: 已初始化 (50首诗词)
```

---

## 启动命令

### 后端
```bash
cd server
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 前端
```bash
cd client
npm run dev
```

### 一键启动 (Windows)
```bash
start.bat
```

### 停止服务
```bash
stop.bat
```

---

## 健康检查

```bash
# 后端健康检查
curl http://localhost:8000/health

# 预期响应
{"status": "healthy"}
```

---

## 回滚方案

如果需要回滚：

```bash
# 1. 停止服务
stop.bat

# 2. 回滚代码
git checkout c83e304

# 3. 重新启动
start.bat
```

---

## 数据库

- 路径: `data/poems.db`
- 类型: SQLite
- 数据: 50首测试诗词

### 导入完整数据
```bash
cd server
python scripts/import_xml.py --file "G:/cnkgraph/CNKGraph.Writings.xml"
```

---

## 监控

### 查看日志
```bash
# 后端日志
tail -f logs/backend.log

# 前端日志
tail -f logs/frontend.log
```

### 查看进程
```bash
# Windows
tasklist | findstr python
tasklist | findstr node

# Linux/Mac
ps aux | grep uvicorn
ps aux | grep vite
```

---

## 性能指标

| 指标 | 值 |
|---|---|
| 后端启动时间 | ~2秒 |
| 前端启动时间 | ~500ms |
| API 响应时间 | <100ms |
| 数据库查询 | <50ms |

---

DP-8: 批准
