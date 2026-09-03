<template>
  <div class="guide">
    <div class="page-header">
      <div>
        <div class="decorative-line"></div>
        <h1 class="page-title">📖 使用说明</h1>
      </div>
    </div>
    
    <!-- 系统简介 -->
    <div class="section-card">
      <div class="section-header">
        <div class="section-title">🎬 系统简介</div>
      </div>
      <div class="guide-content">
        <p>本系统是一个<strong>古诗词短视频自动化生成流水线</strong>，基于 Agnes AI 免费 API，实现从诗词选题到视频成品的全自动生产。</p>
        <p>核心架构：<strong>Generator-Critic 双角色模式</strong> — 同一个 AI 模型扮演"创作者"和"苛刻编辑"两种角色，自动完成内容生成和质量把控。</p>
      </div>
    </div>
    
    <!-- 核心功能 -->
    <div class="section-card">
      <div class="section-header">
        <div class="section-title">⚡ 核心功能</div>
      </div>
      <div class="feature-grid">
        <div class="feature-item">
          <div class="feature-icon">📚</div>
          <div class="feature-title">智能选题</div>
          <div class="feature-desc">从200万首古诗词中筛选，支持按朝代、体裁、作者筛选</div>
        </div>
        <div class="feature-item">
          <div class="feature-icon">✍️</div>
          <div class="feature-title">文案生成</div>
          <div class="feature-desc">AI 自动生成深度解读文案，包含痛点钩子、人设重塑等结构</div>
        </div>
        <div class="feature-item">
          <div class="feature-icon">🎨</div>
          <div class="feature-title">分镜生成</div>
          <div class="feature-desc">自动拆分30+分镜画面，生成对应的图片提示词</div>
        </div>
        <div class="feature-item">
          <div class="feature-icon">🖼️</div>
          <div class="feature-title">图片生成</div>
          <div class="feature-desc">AI 生成写实风格的古风画面，支持批量生成</div>
        </div>
        <div class="feature-item">
          <div class="feature-icon">🎬</div>
          <div class="feature-title">视频合成</div>
          <div class="feature-desc">图片自动转视频片段，支持角色一致性参考</div>
        </div>
        <div class="feature-item">
          <div class="feature-icon">🎯</div>
          <div class="feature-title">质量把控</div>
          <div class="feature-desc">苛刻编辑自动打分，低于阈值自动重做</div>
        </div>
      </div>
    </div>
    
    <!-- 操作流程 -->
    <div class="section-card">
      <div class="section-header">
        <div class="section-title">📋 操作流程</div>
      </div>
      <div class="flow-steps">
        <div class="flow-step">
          <div class="flow-number">1</div>
          <div class="flow-content">
            <div class="flow-title">选择诗词</div>
            <div class="flow-desc">在「选题筛选」页面，从诗词列表中选择想要制作视频的诗词</div>
          </div>
        </div>
        <div class="flow-step">
          <div class="flow-number">2</div>
          <div class="flow-content">
            <div class="flow-title">创建任务</div>
            <div class="flow-desc">点击「创建任务」按钮，系统自动将诗词加入生产队列</div>
          </div>
        </div>
        <div class="flow-step">
          <div class="flow-number">3</div>
          <div class="flow-content">
            <div class="flow-title">自动生产</div>
            <div class="flow-desc">系统自动执行：文案生成 → 文案审核 → 图片生成 → 视频合成</div>
          </div>
        </div>
        <div class="flow-step">
          <div class="flow-number">4</div>
          <div class="flow-content">
            <div class="flow-title">查看进度</div>
            <div class="flow-desc">在「任务管理」页面查看实时进度，点击「详情」查看完整内容</div>
          </div>
        </div>
        <div class="flow-step">
          <div class="flow-number">5</div>
          <div class="flow-content">
            <div class="flow-title">导出成品</div>
            <div class="flow-desc">任务完成后，在任务详情页点击「导出视频」下载成品</div>
          </div>
        </div>
      </div>
    </div>
    
    <!-- 技术架构 -->
    <div class="section-card">
      <div class="section-header">
        <div class="section-title">🏗️ 技术架构</div>
      </div>
      <div class="tech-info">
        <div class="tech-item">
          <span class="tech-label">文本模型</span>
          <span class="tech-value">agnes-2.5-flash（免费）</span>
        </div>
        <div class="tech-item">
          <span class="tech-label">图片模型</span>
          <span class="tech-value">agnes-image-2.1-flash（免费）</span>
        </div>
        <div class="tech-item">
          <span class="tech-label">视频模型</span>
          <span class="tech-value">agnes-video-2.5-flash（免费）</span>
        </div>
        <div class="tech-item">
          <span class="tech-label">数据库</span>
          <span class="tech-value">SQLite（本地存储）</span>
        </div>
        <div class="tech-item">
          <span class="tech-label">并发控制</span>
          <span class="tech-value">文案5 / 图片5 / 视频1</span>
        </div>
      </div>
    </div>
    
    <!-- 注意事项 -->
    <div class="section-card">
      <div class="section-header">
        <div class="section-title">⚠️ 注意事项</div>
      </div>
      <div class="guide-content">
        <ul class="notice-list">
          <li>视频生成 API 限速为 1次/分钟，批量任务需要排队等待</li>
          <li>每个环节都有质量打分，低于阈值（默认7分）会自动重做</li>
          <li>所有中间产物（文案、图片、视频）都会保存在 data/output 目录</li>
          <li>数据库已预导入约50首测试诗词，完整数据需运行导入脚本</li>
        </ul>
      </div>
    </div>
  </div>
</template>

<script setup>
// 使用说明页面，纯静态内容
</script>

<style scoped>
.guide-content {
  line-height: 1.8;
  color: var(--color-text-secondary);
}

.guide-content p {
  margin-bottom: 12px;
}

.feature-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 20px;
}

.feature-item {
  padding: 20px;
  background: var(--color-bg);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-border-light);
  text-align: center;
}

.feature-icon {
  font-size: 32px;
  margin-bottom: 12px;
}

.feature-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: 8px;
}

.feature-desc {
  font-size: 13px;
  color: var(--color-text-muted);
}

.flow-steps {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.flow-step {
  display: flex;
  align-items: flex-start;
  gap: 16px;
  padding: 16px;
  background: var(--color-bg);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-border-light);
}

.flow-number {
  width: 32px;
  height: 32px;
  background: var(--color-primary);
  color: white;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  flex-shrink: 0;
}

.flow-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: 4px;
}

.flow-desc {
  font-size: 14px;
  color: var(--color-text-secondary);
}

.tech-info {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 16px;
}

.tech-item {
  display: flex;
  justify-content: space-between;
  padding: 12px 16px;
  background: var(--color-bg);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-border-light);
}

.tech-label {
  font-weight: 500;
  color: var(--color-text-secondary);
}

.tech-value {
  color: var(--color-primary);
  font-weight: 600;
}

.notice-list {
  padding-left: 20px;
  margin: 0;
}

.notice-list li {
  margin-bottom: 8px;
  color: var(--color-text-secondary);
}
</style>
