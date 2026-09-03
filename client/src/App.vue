<template>
  <div class="app-layout">
    <!-- 侧边栏 -->
    <aside class="sidebar" :class="{ collapsed: sidebarCollapsed }">
      <div class="sidebar-header">
        <div class="logo" v-if="!sidebarCollapsed">
          <span class="logo-icon">🏛️</span>
          <span class="logo-text">古诗词短视频</span>
        </div>
        <span class="logo-icon logo-mini" v-else>🏛️</span>
        <button class="collapse-btn" @click="sidebarCollapsed = !sidebarCollapsed">
          {{ sidebarCollapsed ? '»' : '«' }}
        </button>
      </div>
      
      <nav class="sidebar-nav">
        <router-link to="/hot" class="nav-item" active-class="active">
          <span class="nav-icon">🔥</span>
          <span class="nav-label" v-if="!sidebarCollapsed">热点选题</span>
        </router-link>
        <router-link to="/tasks" class="nav-item" active-class="active">
          <span class="nav-icon">📋</span>
          <span class="nav-label" v-if="!sidebarCollapsed">任务管理</span>
        </router-link>
        <router-link to="/poetry" class="nav-item" active-class="active">
          <span class="nav-icon">📚</span>
          <span class="nav-label" v-if="!sidebarCollapsed">诗词库</span>
        </router-link>
      </nav>
      
      <div class="sidebar-footer">
        <router-link to="/settings" class="nav-item" active-class="active">
          <span class="nav-icon">⚙️</span>
          <span class="nav-label" v-if="!sidebarCollapsed">设置</span>
        </router-link>
      </div>
    </aside>
    
    <!-- 主内容区 -->
    <main class="main-content">
      <router-view />
    </main>
  </div>
</template>

<script setup>
import { ref, onErrorCaptured } from 'vue'

const sidebarCollapsed = ref(false)

// 防御：单个路由页（如任务详情）渲染出错时就地隔离，避免整框架（侧边栏/导航）被卸载
onErrorCaptured((err, instance, info) => {
  console.error('[App] 子组件渲染错误已隔离，框架未受影响：', err, info)
  return false
})
</script>

<style>
/* Design Tokens */
:root {
  /* 颜色系统 - 古铜色系 */
  --color-primary: #8B4513;
  --color-primary-light: #A0522D;
  --color-primary-dark: #6B3410;
  
  /* 背景色 */
  --color-bg: #FAF8F5;
  --color-bg-card: #FFFFFF;
  --color-bg-sidebar: linear-gradient(180deg, #2C1810 0%, #1a0f0a 100%);
  
  /* 文字颜色 */
  --color-text: #2C1810;
  --color-text-secondary: #5D4E37;
  --color-text-muted: #8B7355;
  
  /* 边框 */
  --color-border: #E8E0D5;
  --color-border-light: #F5F0EB;
  
  /* 状态颜色 */
  --color-success: #52C41A;
  --color-warning: #FAAD14;
  --color-error: #FF4D4F;
  --color-info: #1890FF;
  
  /* 间距 */
  --spacing-1: 4px;
  --spacing-2: 8px;
  --spacing-3: 12px;
  --spacing-4: 16px;
  --spacing-5: 20px;
  --spacing-6: 24px;
  
  /* 圆角 */
  --radius-sm: 4px;
  --radius-md: 8px;
  --radius-lg: 12px;
  
  /* 阴影 */
  --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.05);
  --shadow-md: 0 4px 6px rgba(0, 0, 0, 0.07);
  --shadow-lg: 0 10px 15px rgba(0, 0, 0, 0.1);
}

/* 全局样式 */
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

body {
  font-family: 'Noto Serif SC', 'SimSun', serif;
  background-color: var(--color-bg);
  color: var(--color-text);
  line-height: 1.6;
}

/* 布局 */
.app-layout {
  display: flex;
  min-height: 100vh;
}

/* 侧边栏 */
.sidebar {
  width: 220px;
  background: var(--color-bg-sidebar);
  color: #FFF;
  display: flex;
  flex-direction: column;
  transition: width 0.3s ease;
  position: fixed;
  top: 0;
  left: 0;
  bottom: 0;
  z-index: 100;
}

.sidebar.collapsed {
  width: 64px;
}

.sidebar-header {
  padding: var(--spacing-4);
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
}

.logo {
  display: flex;
  align-items: center;
  gap: var(--spacing-3);
}

.logo-icon {
  font-size: 24px;
  line-height: 1;
  display: inline-flex;
  align-items: center;
}

.logo-text {
  font-size: 16px;
  font-weight: 600;
  line-height: 1;
  white-space: nowrap;
}

.collapse-btn {
  background: none;
  border: none;
  color: rgba(255, 255, 255, 0.6);
  cursor: pointer;
  font-size: 16px;
  padding: var(--spacing-1);
}

.collapse-btn:hover {
  color: #FFF;
}

/* 导航 */
.sidebar-nav {
  flex: 1;
  padding: var(--spacing-4) 0;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: var(--spacing-3);
  padding: var(--spacing-3) var(--spacing-4);
  color: rgba(255, 255, 255, 0.7);
  text-decoration: none;
  transition: all 0.2s;
}

.nav-item:hover {
  background: rgba(255, 255, 255, 0.1);
  color: #FFF;
}

.nav-item.active {
  background: rgba(139, 69, 19, 0.3);
  color: #FFF;
  border-left: 3px solid var(--color-primary);
}

.nav-icon {
  font-size: 20px;
}

.nav-label {
  font-size: 14px;
  white-space: nowrap;
}

/* 侧边栏底部 */
.sidebar-footer {
  padding: var(--spacing-4) 0;
  border-top: 1px solid rgba(255, 255, 255, 0.1);
}

/* 主内容区 */
.main-content {
  flex: 1;
  margin-left: 220px;
  padding: var(--spacing-6);
  transition: margin-left 0.3s ease;
}

.sidebar.collapsed ~ .main-content {
  margin-left: 64px;
}

/* 通用组件样式 */
.btn {
  padding: var(--spacing-2) var(--spacing-4);
  border-radius: var(--radius-md);
  font-size: 14px;
  cursor: pointer;
  transition: all 0.2s;
  border: none;
  display: inline-flex;
  align-items: center;
  gap: var(--spacing-2);
}

.btn-primary {
  background: var(--color-primary);
  color: #FFF;
}

.btn-primary:hover {
  background: var(--color-primary-light);
}

.btn-secondary {
  background: var(--color-border);
  color: var(--color-text);
}

.btn-secondary:hover {
  background: var(--color-border-light);
}

.btn-sm {
  padding: var(--spacing-1) var(--spacing-3);
  font-size: 12px;
}

/* 状态标签 */
.status-tag {
  display: inline-flex;
  align-items: center;
  gap: var(--spacing-1);
  padding: var(--spacing-1) var(--spacing-2);
  border-radius: var(--radius-sm);
  font-size: 12px;
}

.status-pending {
  background: #FFF7E6;
  color: #D48806;
}

.status-processing {
  background: #E6F7FF;
  color: #1890FF;
}

.status-done {
  background: #F6FFED;
  color: #52C41A;
}

.status-failed {
  background: #FFF2F0;
  color: #FF4D4F;
}

/* 分隔线 */
.decorative-line {
  width: 40px;
  height: 3px;
  background: var(--color-primary);
  border-radius: 2px;
  margin-bottom: var(--spacing-2);
}

/* 页面标题 */
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: var(--spacing-6);
}

.page-title {
  font-size: 24px;
  font-weight: 600;
  color: var(--color-text);
}

/* 卡片样式 */
.section-card {
  background: var(--color-bg-card);
  border-radius: var(--radius-lg);
  padding: var(--spacing-5);
  margin-bottom: var(--spacing-4);
  box-shadow: var(--shadow-sm);
  border: 1px solid var(--color-border-light);
}

.section-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: var(--spacing-4);
}

.section-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
}

/* 按钮组 */
.btn-group {
  display: flex;
  gap: var(--spacing-2);
}
</style>
