<script setup lang="ts">
import { computed } from 'vue'

import AppNav from '@/components/AppNav.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const displayName = computed(() => auth.user?.display_name || auth.username)
</script>

<template>
  <div class="shell">
    <aside class="shell-sidebar">
      <div class="shell-brand">OpsPilot</div>
      <AppNav />
    </aside>

    <div class="shell-main">
      <header class="shell-header">
        <div class="shell-header-spacer" />
        <div class="shell-user">
          <span class="shell-username">{{ displayName }}</span>
          <a class="shell-logout" role="button" tabindex="0" @click="auth.logout()">退出</a>
        </div>
      </header>

      <main class="shell-content">
        <router-view />
      </main>
    </div>
  </div>
</template>

<style scoped>
.shell {
  display: flex;
  min-height: 100%;
}

.shell-sidebar {
  display: flex;
  flex-direction: column;
  width: 220px;
  flex-shrink: 0;
  background: var(--opspilot-sidebar);
  color: var(--opspilot-sidebar-text);
}

.shell-brand {
  padding: 18px 20px;
  color: var(--opspilot-sidebar-active);
  font-size: 18px;
  font-weight: 600;
  letter-spacing: 0.02em;
}

.shell-main {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
}

.shell-header {
  display: flex;
  align-items: center;
  height: 56px;
  padding: 0 24px;
  background: #fff;
  border-bottom: 1px solid var(--opspilot-border);
}

.shell-header-spacer {
  flex: 1;
}

.shell-user {
  display: flex;
  align-items: center;
  gap: 12px;
}

.shell-username {
  color: #606266;
}

.shell-logout {
  color: #409eff;
  cursor: pointer;
  user-select: none;
}

.shell-content {
  flex: 1;
  padding: 24px;
}
</style>
