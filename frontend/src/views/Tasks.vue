<script setup lang="ts">
/**
 * S2 task list: what was asked of the platform, and how it ended.
 *
 * This page only reads. Submitting work happens where the work is described —
 * the server check button on the inventory page today, templates and
 * deployments later — so there is deliberately no "new task" form here.
 */
import { onMounted, onUnmounted, ref } from 'vue'

import { listTasks } from '@/api/tasks'
import RequestError from '@/components/RequestError.vue'
import type { TaskOut, TaskStatus } from '@/types/api'

const tasks = ref<TaskOut[]>([])
const loading = ref(false)
const loadError = ref<unknown>(null)
const statusFilter = ref<TaskStatus | ''>('')

// Poll just often enough for the list to feel live without hammering the API.
let timer: number | null = null

const STATUS_OPTIONS: { value: TaskStatus | ''; label: string }[] = [
  { value: '', label: '全部状态' },
  { value: 'RUNNING', label: '执行中' },
  { value: 'QUEUED', label: '已排队' },
  { value: 'PENDING', label: '待处理' },
  { value: 'CANCELLING', label: '取消中' },
  { value: 'SUCCESS', label: '成功' },
  { value: 'PARTIAL_SUCCESS', label: '部分成功' },
  { value: 'FAILED', label: '失败' },
  { value: 'CANCELLED', label: '已取消' },
]

async function load(): Promise<void> {
  loading.value = true
  try {
    const all = await listTasks({ limit: 100 })
    tasks.value = statusFilter.value === '' ? all : all.filter((t) => t.status === statusFilter.value)
    loadError.value = null
  } catch (cause) {
    loadError.value = cause
  } finally {
    loading.value = false
  }
}

function statusLabel(status: string): string {
  return STATUS_OPTIONS.find((o) => o.value === status)?.label ?? status
}

function statusClass(status: string): string {
  return `pill-${status.toLowerCase().replace(/_/g, '-')}`
}

function formatTime(value: string | null): string {
  if (value === null || value === '') return '—'
  return new Date(value).toLocaleString()
}

function duration(task: TaskOut): string {
  if (task.started_at === null) return '—'
  const end = task.finished_at === null ? Date.now() : new Date(task.finished_at).getTime()
  const ms = end - new Date(task.started_at).getTime()
  if (ms < 0) return '—'
  if (ms < 1000) return `${ms} ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`
  return `${Math.floor(ms / 60_000)} 分 ${Math.round((ms % 60_000) / 1000)} 秒`
}

function isTerminal(status: string): boolean {
  return ['SUCCESS', 'PARTIAL_SUCCESS', 'FAILED', 'CANCELLED'].includes(status)
}

onMounted(() => {
  void load()
  timer = window.setInterval(() => void load(), 5000)
})

onUnmounted(() => {
  if (timer !== null) window.clearInterval(timer)
})
</script>

<template>
  <section class="tasks">
    <header class="tasks-header">
      <div>
        <h1 class="tasks-title">任务</h1>
        <p class="tasks-hint">
          平台里每一次远程执行都是一条任务记录。点击进入可以看到每台服务器、每个步骤的状态和完整日志。
        </p>
      </div>
      <label class="filter">
        <span class="filter-label">状态</span>
        <select v-model="statusFilter" class="filter-input" @change="void load()">
          <option v-for="o in STATUS_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
        </select>
      </label>
    </header>

    <RequestError v-if="loadError !== null" :error="loadError" />

    <div v-if="loading && tasks.length === 0" class="tasks-empty">加载中…</div>
    <div v-else-if="tasks.length === 0" class="tasks-empty">
      没有任务记录。在服务器页点「检查」会排入第一条任务。
    </div>

    <table v-else class="tasks-table">
      <thead>
        <tr>
          <th>ID</th>
          <th>类型</th>
          <th>标题</th>
          <th>状态</th>
          <th>发起人</th>
          <th>开始</th>
          <th>耗时</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="task in tasks" :key="task.id">
          <td class="mono">#{{ task.id }}</td>
          <td class="mono">{{ task.kind }}</td>
          <td>
            <router-link class="task-link" :to="{ name: 'task-detail', params: { id: task.id } }">
              {{ task.title || `任务 ${task.id}` }}
            </router-link>
            <div v-if="task.retry_of_task_id !== null" class="task-sub">
              重试自 #{{ task.retry_of_task_id }}
            </div>
          </td>
          <td>
            <span class="pill" :class="statusClass(task.status)">{{ statusLabel(task.status) }}</span>
            <div v-if="task.cancel_requested_at !== null && !isTerminal(task.status)" class="task-sub">
              已请求取消
            </div>
          </td>
          <td>{{ task.requested_by_name || '—' }}</td>
          <td>{{ formatTime(task.started_at) }}</td>
          <td>{{ duration(task) }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<style scoped>
.tasks-title {
  margin: 0 0 4px;
  font-size: 20px;
  font-weight: 600;
}

.tasks-hint {
  margin: 0;
  color: #909399;
}

.tasks-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 20px;
}

.filter {
  display: flex;
  align-items: center;
  gap: 8px;
}

.filter-label {
  color: #606266;
  font-size: 13px;
}

.filter-input {
  padding: 8px 10px;
  border: 1px solid var(--opspilot-border);
  border-radius: 4px;
  font-size: 13px;
  background: #fff;
}

.tasks-empty {
  padding: 32px;
  color: #909399;
  text-align: center;
  background: #fff;
  border: 1px solid var(--opspilot-border);
  border-radius: 8px;
}

.tasks-table {
  width: 100%;
  background: #fff;
  border: 1px solid var(--opspilot-border);
  border-collapse: collapse;
  border-radius: 8px;
}

.tasks-table th,
.tasks-table td {
  padding: 12px 14px;
  border-bottom: 1px solid var(--opspilot-border);
  font-size: 13px;
  text-align: left;
  vertical-align: top;
}

.tasks-table th {
  color: #909399;
  font-weight: 500;
  background: #fafafa;
}

.tasks-table tr:last-child td {
  border-bottom: none;
}

.task-link {
  color: #409eff;
  font-weight: 600;
  text-decoration: none;
}

.task-link:hover {
  text-decoration: underline;
}

.task-sub {
  margin-top: 2px;
  color: #909399;
  font-size: 12px;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  word-break: break-all;
}

.pill {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
}

.pill-success {
  color: #67c23a;
  background: #f0f9eb;
}

.pill-partial-success {
  color: #e6a23c;
  background: #fdf6ec;
}

.pill-failed {
  color: #f56c6c;
  background: #fef0f0;
}

.pill-cancelled,
.pill-cancelling {
  color: #909399;
  background: #f4f4f5;
}

.pill-running {
  color: #409eff;
  background: #ecf5ff;
}

.pill-queued,
.pill-pending {
  color: #909399;
  background: #f4f4f5;
}
</style>
