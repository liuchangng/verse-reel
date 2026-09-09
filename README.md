# 古诗词短视频工厂

基于 Agnes AI 免费 API 的古诗词短视频自动化生产系统。

## 功能特性

- 🎯 **智能选题**：从200万+首古诗词中筛选
- 📝 **AI文案**：Generator-Critic 双角色，自动生成+评分
- 🎨 **AI图片**：分镜自动生成，质量评分
- 🎬 **AI视频**：图生视频，多平台适配
- 🖥️ **Web管理**：可视化进度监控

## 技术栈

### 后端 (server/)
- **Python 3.12（uv 托管）**
- **uv** - 依赖管理与虚拟环境
- **FastAPI** - Web 框架
- **SQLAlchemy** - ORM
- **OpenCC** - 繁简转换
- **httpx** - HTTP 客户端

### 前端 (client/)
- **Vue 3** - UI 框架
- **Arco Design** - 组件库
- **Vite** - 构建工具
- **Pinia** - 状态管理

### AI 服务
- **agnes-2.5-flash** - 文本生成（免费）
- **agnes-image-2.1-flash** - 图片生成（免费）
- **agnes-video-2.5-flash** - 视频生成（免费）

## 快速开始

### 1. 环境要求

- uv（含 Python 3.12 托管，后端无需全局 Python）
- Node.js 18+
- Agnes AI API Key

### 2. 安装依赖

后端 Python 依赖统一由 uv 管理（基于 `server/pyproject.toml` + `server/uv.lock`）：

```bash
# 后端（核心依赖）
cd server
uv sync

# 如需启用 CosyVoice 高质量 TTS（可选）
uv sync --extra cosyvoice

# 前端
cd client
npm install
```

### 3. 配置

复制 `server/.env.example` 为 `server/.env`，填入你的 API Key。

### 4. 启动

```bash
# Windows
start.bat

# 或手动启动
# 后端（uv 托管，使用 server/.venv）
cd server
uv run uvicorn app.main:app --reload

# 前端
cd client
npm run dev
```

### 5. 导入数据

```bash
cd server
python scripts/import_xml.py --file "G:/cnkgraph/CNKGraph.Writings.xml"
```

## 项目结构

```
├── server/                 # 后端
│   ├── app/
│   │   ├── api/           # API 路由
│   │   ├── models/        # 数据模型
│   │   ├── services/      # 业务逻辑
│   │   └── utils/         # 工具函数
│   ├── scripts/           # 脚本
│   ├── pyproject.toml     # uv 依赖声明（核心 + cosyvoice extra）
│   └── uv.lock            # uv 锁定文件（唯一事实源）
├── client/                 # 前端
│   ├── src/
│   │   ├── views/         # 页面
│   │   ├── api/           # API 客户端
│   │   └── styles/        # 样式
│   └── package.json
└── README.md
```

## API 接口

- `GET /api/poems` - 诗词列表
- `GET /api/poems/stats` - 诗词统计
- `GET /api/tasks` - 任务列表
- `POST /api/tasks` - 创建任务
- `POST /api/tasks/{id}/start` - 启动任务
- `WS /ws/progress/{task_id}` - 实时进度

## 开发说明

### Generator-Critic 架构

系统使用同一个大模型（agnes-2.5-flash）扮演两个角色：

1. **生成者**：负责生成文案、分镜提示词
2. **评判者**：负责评分、给出改进建议

这种设计确保了审美标准的一致性，同时降低了成本。

### 并发控制

- 文案生成：5 并发
- 图片生成：5 并发
- 视频生成：1 并发（API 限速）
- 评分：5 并发

### 质检流程

1. 生成内容
2. 模型评分（0-10分）
3. 低于阈值（默认7分）自动重写
4. 最多重试3次

## License

MIT
