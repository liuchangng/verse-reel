import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  {
    path: '/',
    redirect: '/hot',
  },
  {
    path: '/hot',
    name: 'HotTopics',
    component: () => import('../views/HotTopics.vue'),
    meta: { title: '热点选题' },
  },
  {
    path: '/tasks',
    name: 'TaskList',
    component: () => import('../views/TaskList.vue'),
    meta: { title: '任务管理' },
  },
  {
    path: '/tasks/:id',
    name: 'TaskDetail',
    component: () => import('../views/TaskDetail.vue'),
    meta: { title: '任务详情' },
  },
  {
    path: '/poetry',
    name: 'PoetryLibrary',
    component: () => import('../views/PoetryLibrary.vue'),
    meta: { title: '诗词库' },
  },
  {
    path: '/settings',
    name: 'Settings',
    component: () => import('../views/Settings.vue'),
    meta: { title: '系统设置' },
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
