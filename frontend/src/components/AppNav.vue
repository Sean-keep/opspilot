<script setup lang="ts">
/**
 * Sidebar navigation.
 *
 * Menu visibility is driven by the permission list purely so an operator is
 * not offered something they cannot use. It is **not** an authorisation
 * boundary — the backend refuses the same operations regardless of what is
 * shown here.
 */
import { computed } from 'vue'
import { useRoute } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

interface NavItem {
  label: string
  /** Route name. ``null`` means "not built yet" and renders as disabled. */
  routeName: string | null
  /** Show only when the user holds at least one of these. Empty = always. */
  permissions: readonly string[]
}

const NAV_ITEMS: readonly NavItem[] = [
  { label: '运行状态', routeName: 'dashboard', permissions: [] },
  { label: '服务器', routeName: 'servers', permissions: ['server.read'] },
  { label: '凭据', routeName: 'credentials', permissions: ['credential.read'] },
  { label: '模板', routeName: null, permissions: ['template.read'] },
  { label: '任务', routeName: 'tasks', permissions: ['task.read'] },
  { label: '应用', routeName: null, permissions: ['app.read'] },
  { label: '发布记录', routeName: null, permissions: ['deployment.read'] },
  { label: '审计日志', routeName: null, permissions: ['audit.read'] },
]

const auth = useAuthStore()
const route = useRoute()

const visibleItems = computed(() =>
  NAV_ITEMS.filter((item) => item.permissions.length === 0 || auth.hasAnyPermission(item.permissions)),
)

function isActive(item: NavItem): boolean {
  return item.routeName !== null && route.name === item.routeName
}
</script>

<template>
  <nav class="app-nav" aria-label="主导航">
    <ul class="app-nav-list">
      <li v-for="item in visibleItems" :key="item.label">
        <router-link
          v-if="item.routeName"
          class="app-nav-link"
          :class="{ active: isActive(item) }"
          :to="{ name: item.routeName }"
        >
          {{ item.label }}
        </router-link>
        <span v-else class="app-nav-link disabled" aria-disabled="true">
          {{ item.label }}
          <span class="app-nav-soon">即将开放</span>
        </span>
      </li>
    </ul>
  </nav>
</template>

<style scoped>
.app-nav-list {
  margin: 0;
  padding: 8px 0;
  list-style: none;
}

.app-nav-link {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 11px 20px;
  color: var(--opspilot-sidebar-text);
  font-size: 14px;
  text-decoration: none;
  cursor: pointer;
}

.app-nav-link:hover {
  color: var(--opspilot-sidebar-active);
  background: rgba(255, 255, 255, 0.06);
}

.app-nav-link.active {
  color: var(--opspilot-sidebar-active);
  background: rgba(255, 255, 255, 0.1);
  box-shadow: inset 3px 0 0 #409eff;
}

.app-nav-link.disabled {
  cursor: default;
  opacity: 0.55;
}

.app-nav-link.disabled:hover {
  color: var(--opspilot-sidebar-text);
  background: transparent;
}

.app-nav-soon {
  font-size: 11px;
  opacity: 0.8;
}
</style>
