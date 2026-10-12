import { createRouter, createWebHistory } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/Login.vue'),
      meta: { public: true },
    },
    {
      path: '/',
      name: 'shell',
      component: () => import('@/views/AppShell.vue'),
      children: [
        {
          path: '',
          name: 'dashboard',
          component: () => import('@/views/Dashboard.vue'),
        },
        {
          path: 'servers',
          name: 'servers',
          component: () => import('@/views/Servers.vue'),
        },
        {
          path: 'credentials',
          name: 'credentials',
          component: () => import('@/views/Credentials.vue'),
        },
        {
          path: 'applications',
          name: 'applications',
          component: () => import('@/views/Applications.vue'),
        },
        {
          path: 'deployments',
          name: 'deployments',
          component: () => import('@/views/Deployments.vue'),
        },
        {
          path: 'tasks',
          name: 'tasks',
          component: () => import('@/views/Tasks.vue'),
        },
        {
          path: 'tasks/:id',
          name: 'task-detail',
          component: () => import('@/views/TaskDetail.vue'),
        },
      ],
    },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()

  if (to.meta.public === true) {
    return true
  }

  if (!auth.isAuthenticated) {
    // Restore identity from a stored token so a refresh does not log you out.
    if (auth.token !== null) {
      try {
        await auth.refresh()
      } catch {
        auth.logout()
      }
    }
    if (!auth.isAuthenticated) {
      return { name: 'login', query: { redirect: to.fullPath } }
    }
  }

  return true
})

export default router
