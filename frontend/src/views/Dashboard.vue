<script setup lang="ts">
/**
 * S0 dashboard: process health only. Inventory, tasks, and deployments each
 * get their own page in later stages — this is deliberately a placeholder
 * rather than a fake summary of things that do not exist yet.
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'

import { api, ApiError, NetworkError } from '@/api/client'
import type { HealthzOut } from '@/types/api'

const health = ref<HealthzOut | null>(null)
const loading = ref(false)
const loadError = ref<string | null>(null)
let timer: number | null = null

const dbOk = computed(() => health.value?.database === 'ok')

async function loadHealth(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    health.value = await api.get<HealthzOut>('/healthz')
  } catch (cause) {
    if (cause instanceof ApiError || cause instanceof NetworkError) {
      loadError.value = cause.message
    } else {
      loadError.value = 'health check failed'
    }
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await loadHealth()
  timer = window.setInterval(() => void loadHealth(), 30_000)
})

onUnmounted(() => {
  if (timer !== null) {
    window.clearInterval(timer)
    timer = null
  }
})
</script>

<template>
  <section class="dashboard">
    <h1 class="dashboard-title">运行状态</h1>
    <p class="dashboard-hint">
      S0 只提供控制台骨架：登录、权限、审计与健康检查。服务器资产、任务编排与应用发布在后续阶段接入。
    </p>

    <div class="dashboard-cards">
      <div class="card">
        <h2 class="card-title">API</h2>
        <p class="card-value" :class="health ? 'ok' : 'unknown'">
          {{ health ? health.status : loading ? '检查中…' : '未知' }}
        </p>
        <p v-if="health" class="card-meta">环境 {{ health.env }} · {{ health.time }}</p>
      </div>

      <div class="card">
        <h2 class="card-title">元数据库</h2>
        <p class="card-value" :class="dbOk ? 'ok' : 'bad'">
          {{ health ? (dbOk ? '可达' : '不可达') : loading ? '检查中…' : '未知' }}
        </p>
        <p class="card-meta">MySQL</p>
      </div>

      <div class="card">
        <h2 class="card-title">Worker</h2>
        <p class="card-value unknown">S2 接入</p>
        <p class="card-meta">独立进程，租约认领</p>
      </div>
    </div>

    <p v-if="loadError" class="dashboard-error" role="alert">{{ loadError }}</p>
  </section>
</template>

<style scoped>
.dashboard-title {
  margin: 0 0 4px;
  font-size: 20px;
  font-weight: 600;
}

.dashboard-hint {
  margin: 0 0 24px;
  color: #909399;
}

.dashboard-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 16px;
}

.card {
  padding: 20px;
  background: #fff;
  border: 1px solid var(--opspilot-border);
  border-radius: 8px;
}

.card-title {
  margin: 0 0 8px;
  color: #909399;
  font-size: 13px;
  font-weight: 500;
}

.card-value {
  margin: 0 0 6px;
  font-size: 22px;
  font-weight: 600;
}

.card-value.ok {
  color: #67c23a;
}

.card-value.bad {
  color: #f56c6c;
}

.card-value.unknown {
  color: #909399;
}

.card-meta {
  margin: 0;
  color: #b1b3b8;
  font-size: 12px;
}

.dashboard-error {
  margin-top: 16px;
  color: #f56c6c;
}
</style>
