<script setup lang="ts">
/**
 * S4 deployment history: what was released, where, and how it ended.
 *
 * Submitting a release and rolling one back both start *here*, and both are
 * deliberate acts with a pinned version and a checksum. The form asks for the
 * release, the target servers and the non-secret config; secret entries come
 * from the environment's `config_ref` and are resolved by the worker, so there
 * is no field for them on this page.
 *
 * A rollback is a new deployment, not an edit — and it needs `confirm`. That
 * check is enforced by the backend; the checkbox here is the second half of a
 * double confirmation, not the security control.
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { listApplications } from '@/api/applications'
import { deployRelease, listDeployments, rollbackDeployment } from '@/api/deployments'
import { listServers } from '@/api/inventory'
import PermissionGate from '@/components/PermissionGate.vue'
import RequestError from '@/components/RequestError.vue'
import { useAuthStore } from '@/stores/auth'
import type {
  AppEnvironmentName,
  ApplicationOut,
  DeploymentOut,
  DeploymentStatus,
  ServerOut,
} from '@/types/api'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const deployments = ref<DeploymentOut[]>([])
const apps = ref<ApplicationOut[]>([])
const servers = ref<ServerOut[]>([])
const loading = ref(false)
const loadError = ref<unknown>(null)
const actionError = ref<unknown>(null)
const busy = ref(false)

const appFilter = ref<number | ''>('')
const statusFilter = ref<DeploymentStatus | ''>('')

// -- deploy form -----------------------------------------------------------
const showDeployForm = ref(false)
const formAppId = ref<number | ''>('')
const formEnv = ref<AppEnvironmentName | ''>('')
const formRelease = ref('')
const formArtifactUrl = ref('')
const formChecksum = ref('')
const formTargets = ref<Set<number>>(new Set())
const formModule = ref('')
const formPort = ref(8000)
const formPython = ref('3.12')
const formRequirements = ref('requirements.lock')
const configRows = ref<{ key: string; value: string }[]>([{ key: '', value: '' }])

// -- rollback form ---------------------------------------------------------
const rollbackTarget = ref<DeploymentOut | null>(null)
const rollbackTargets = ref<Set<number>>(new Set())
const rollbackReason = ref('')
const rollbackConfirmed = ref(false)

const STATUS_OPTIONS: { value: DeploymentStatus | ''; label: string }[] = [
  { value: '', label: '全部状态' },
  { value: 'PENDING', label: '待处理' },
  { value: 'RUNNING', label: '执行中' },
  { value: 'SUCCEEDED', label: '成功' },
  { value: 'ROLLED_BACK', label: '已回滚' },
  { value: 'FAILED', label: '失败' },
  { value: 'CANCELLED', label: '已取消' },
]

const TERMINAL: readonly DeploymentStatus[] = ['SUCCEEDED', 'FAILED', 'ROLLED_BACK', 'CANCELLED']

let timer: number | null = null

const formEnvironments = computed(() => {
  const app = apps.value.find((a) => a.id === formAppId.value)
  return app === undefined ? [] : app.environments
})

const filtered = computed(() => {
  return deployments.value.filter((d) => {
    if (appFilter.value !== '' && d.application_id !== appFilter.value) return false
    if (statusFilter.value !== '' && d.status !== statusFilter.value) return false
    return true
  })
})

const canRollback = computed(() => auth.hasPermission('deployment.rollback'))

function appName(id: number): string {
  return apps.value.find((a) => a.id === id)?.name ?? `应用 ${id}`
}

function envName(id: number): string {
  for (const app of apps.value) {
    const env = app.environments.find((e) => e.id === id)
    if (env !== undefined) return env.name
  }
  return `环境 ${id}`
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

function isTerminal(status: DeploymentStatus): boolean {
  return TERMINAL.includes(status)
}

/**
 * Rollback is offered only when there is something to put back and nothing
 * still in flight. The backend enforces both; this just does not offer the
 * button.
 */
function canRollbackRow(row: DeploymentOut): boolean {
  return canRollback.value && isTerminal(row.status) && row.previous_release_id !== ''
}

function toggleTarget(set: Set<number>, id: number): Set<number> {
  const next = new Set(set)
  if (next.has(id)) {
    next.delete(id)
  } else {
    next.add(id)
  }
  return next
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const [all, allApps, allServers] = await Promise.all([
      listDeployments({ limit: 100 }),
      listApplications(),
      listServers(),
    ])
    deployments.value = all
    apps.value = allApps
    servers.value = allServers
    loadError.value = null
  } catch (cause) {
    loadError.value = cause
  } finally {
    loading.value = false
  }
}

function addConfigRow(): void {
  configRows.value = [...configRows.value, { key: '', value: '' }]
}

function removeConfigRow(index: number): void {
  configRows.value = configRows.value.filter((_, i) => i !== index)
}

function openDeployForm(): void {
  actionError.value = null
  showDeployForm.value = true
  // Prefill from ?app=…&env=… when the operator came from the applications page.
  const fromApp = Number(route.query.app)
  if (Number.isInteger(fromApp) && apps.value.some((a) => a.id === fromApp)) {
    formAppId.value = fromApp
  }
  const fromEnv = route.query.env
  if (typeof fromEnv === 'string' && fromEnv !== '') {
    formEnv.value = fromEnv as AppEnvironmentName
  }
}

function onAppChange(): void {
  // The environment list depends on the application; keep only a valid one.
  const names = formEnvironments.value.map((e) => e.name)
  if (formEnv.value !== '' && !names.includes(formEnv.value)) {
    formEnv.value = ''
  }
  formTargets.value = new Set()
}

function configPayload(): Record<string, string> {
  const out: Record<string, string> = {}
  for (const row of configRows.value) {
    const key = row.key.trim()
    if (key !== '') out[key] = row.value
  }
  return out
}

async function submitDeploy(): Promise<void> {
  const appId = formAppId.value
  const env = formEnv.value
  if (appId === '' || env === '') return
  busy.value = true
  actionError.value = null
  try {
    const body = {
      environment: env,
      release_version: formRelease.value.trim(),
      artifact_url: formArtifactUrl.value.trim(),
      artifact_checksum: formChecksum.value.trim().toLowerCase(),
      targets: [...formTargets.value].map((server_id) => ({ server_id })),
      module_name: formModule.value.trim(),
      app_port: formPort.value,
      python_version: formPython.value.trim(),
      requirements: formRequirements.value.trim(),
      config: configPayload(),
    }
    const created = await deployRelease(appId, body)
    showDeployForm.value = false
    await load()
    if (created.task_id !== null) {
      void router.push({ name: 'task-detail', params: { id: created.task_id } })
    }
  } catch (cause) {
    actionError.value = cause
  } finally {
    busy.value = false
  }
}

function openRollback(row: DeploymentOut): void {
  rollbackTarget.value = row
  rollbackTargets.value = new Set()
  rollbackReason.value = ''
  rollbackConfirmed.value = false
  actionError.value = null
}

async function submitRollback(): Promise<void> {
  const row = rollbackTarget.value
  if (row === null || !rollbackConfirmed.value) return
  busy.value = true
  actionError.value = null
  try {
    const reason = rollbackReason.value.trim()
    const body = {
      targets: [...rollbackTargets.value].map((server_id) => ({ server_id })),
      confirm: true as const,
      ...(reason === '' ? {} : { reason }),
    }
    const created = await rollbackDeployment(row.id, body)
    rollbackTarget.value = null
    await load()
    if (created.task_id !== null) {
      void router.push({ name: 'task-detail', params: { id: created.task_id } })
    }
  } catch (cause) {
    actionError.value = cause
  } finally {
    busy.value = false
  }
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
  <section class="deps">
    <header class="deps-header">
      <div>
        <h1 class="deps-title">发布记录</h1>
        <p class="deps-hint">
          每一次发布都是一个钉死版本号 + 校验和的记录。提交后不会立刻安装，而是排成一条任务，
          步骤和日志在任务详情里按服务器逐条展开。
        </p>
      </div>
      <PermissionGate permission="deployment.create">
        <button type="button" class="btn btn-primary" @click="openDeployForm">新建发布</button>
      </PermissionGate>
    </header>

    <div class="filters">
      <label class="filter">
        <span class="filter-label">应用</span>
        <select v-model="appFilter" class="filter-input">
          <option value="">全部应用</option>
          <option v-for="app in apps" :key="app.id" :value="app.id">{{ app.name }}</option>
        </select>
      </label>
      <label class="filter">
        <span class="filter-label">状态</span>
        <select v-model="statusFilter" class="filter-input">
          <option v-for="o in STATUS_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
        </select>
      </label>
    </div>

    <RequestError v-if="loadError !== null" :error="loadError" />
    <RequestError v-if="actionError !== null" :error="actionError" />

    <!-- ---------------------------------------------------------------- -->
    <div v-if="showDeployForm" class="panel">
      <h2 class="panel-title">新建发布</h2>
      <p class="panel-hint">
        版本号必须是钉死的标识（tag / commit / 构建号）；<code>latest</code>、<code>current</code>、
        <code>shared</code> 等会被后端直接拒绝。密文不要填在这里——环境的配置引用会由 worker 解析。
      </p>
      <div class="form-grid">
        <label class="field">
          <span class="field-label">应用</span>
          <select v-model="formAppId" class="field-input" @change="onAppChange">
            <option value="">选择应用</option>
            <option v-for="app in apps" :key="app.id" :value="app.id">{{ app.name }}</option>
          </select>
        </label>
        <label class="field">
          <span class="field-label">环境</span>
          <select v-model="formEnv" class="field-input">
            <option value="">选择环境</option>
            <option v-for="env in formEnvironments" :key="env.id" :value="env.name">
              {{ env.name }}
            </option>
          </select>
        </label>
        <label class="field">
          <span class="field-label">版本号</span>
          <input v-model="formRelease" class="field-input mono" placeholder="1.4.2" />
        </label>
        <label class="field">
          <span class="field-label">制品地址</span>
          <input
            v-model="formArtifactUrl"
            class="field-input mono"
            placeholder="https://artifacts.example.com/app-1.4.2.tar.gz"
          />
        </label>
        <label class="field">
          <span class="field-label">SHA-256 校验和</span>
          <input
            v-model="formChecksum"
            class="field-input mono"
            placeholder="64 位十六进制"
            maxlength="64"
          />
        </label>
        <label class="field">
          <span class="field-label">入口模块</span>
          <input v-model="formModule" class="field-input mono" placeholder="billing.main" />
        </label>
        <label class="field">
          <span class="field-label">服务端口</span>
          <input v-model.number="formPort" type="number" min="1" max="65535" class="field-input" />
        </label>
        <label class="field">
          <span class="field-label">Python 版本</span>
          <input v-model="formPython" class="field-input mono" placeholder="3.12" />
        </label>
        <label class="field">
          <span class="field-label">依赖锁定文件</span>
          <input v-model="formRequirements" class="field-input mono" placeholder="requirements.lock" />
        </label>
      </div>

      <h3 class="sub-title">目标服务器</h3>
      <div v-if="servers.length === 0" class="muted">
        还没有服务器。先到服务器页接入一台。
      </div>
      <div v-else class="target-list">
        <label v-for="server in servers" :key="server.id" class="target-item">
          <input
            type="checkbox"
            :checked="formTargets.has(server.id)"
            @change="formTargets = toggleTarget(formTargets, server.id)"
          />
          <span class="mono">{{ server.name }}</span>
          <span class="muted">{{ server.hostname }}:{{ server.port }}</span>
        </label>
      </div>

      <h3 class="sub-title">非密文环境变量</h3>
      <div v-for="(row, index) in configRows" :key="index" class="config-row">
        <input v-model="row.key" class="field-input mono" placeholder="LOG_LEVEL" />
        <input v-model="row.value" class="field-input mono" placeholder="info" />
        <button type="button" class="btn btn-small" @click="removeConfigRow(index)">删除</button>
      </div>
      <button type="button" class="btn btn-small" @click="addConfigRow">再加一项</button>

      <div class="form-actions">
        <button type="button" class="btn" :disabled="busy" @click="showDeployForm = false">
          取消
        </button>
        <button
          type="button"
          class="btn btn-primary"
          :disabled="busy || formTargets.size === 0"
          @click="void submitDeploy()"
        >
          提交发布
        </button>
      </div>
    </div>

    <!-- ---------------------------------------------------------------- -->
    <div v-if="loading && deployments.length === 0" class="deps-empty">加载中…</div>
    <div v-else-if="filtered.length === 0" class="deps-empty">
      还没有发布记录。点「新建发布」把一个钉死的版本发到某个环境上。
    </div>

    <table v-else class="deps-table">
      <thead>
        <tr>
          <th>ID</th>
          <th>应用 / 环境</th>
          <th>版本</th>
          <th>类型</th>
          <th>状态</th>
          <th>发起人</th>
          <th>时间</th>
          <th>任务</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in filtered" :key="row.id">
          <td class="mono">#{{ row.id }}</td>
          <td>
            <div>{{ appName(row.application_id) }}</div>
            <div class="muted">{{ envName(row.environment_id) }}</div>
          </td>
          <td class="mono">
            <div>{{ row.release_id }}</div>
            <div v-if="row.previous_release_id" class="muted">
              之前 {{ row.previous_release_id }}
            </div>
          </td>
          <td>
            <span class="kind" :class="{ undo: row.kind === 'ROLLBACK' }">{{ row.kind }}</span>
            <div v-if="row.rollback_of !== null" class="muted">撤销 #{{ row.rollback_of }}</div>
          </td>
          <td>
            <span class="pill" :class="statusClass(row.status)">{{ statusLabel(row.status) }}</span>
            <div v-if="row.error_detail" class="error-detail">{{ row.error_detail }}</div>
          </td>
          <td>{{ row.requested_by || '—' }}</td>
          <td>{{ formatTime(row.created_at) }}</td>
          <td>
            <router-link
              v-if="row.task_id !== null"
              class="task-link"
              :to="{ name: 'task-detail', params: { id: row.task_id } }"
            >
              任务 #{{ row.task_id }}
            </router-link>
            <span v-else class="muted">—</span>
          </td>
          <td class="row-actions">
            <button
              v-if="canRollbackRow(row)"
              type="button"
              class="btn btn-small btn-danger"
              @click="openRollback(row)"
            >
              回滚
            </button>
          </td>
        </tr>
      </tbody>
    </table>

    <!-- ---------------------------------------------------------------- -->
    <div v-if="rollbackTarget !== null" class="panel panel-danger">
      <h2 class="panel-title">回滚 #{{ rollbackTarget.id }}</h2>
      <p class="panel-hint">
        这会创建一条<strong>新的</strong>发布记录，把
        <code class="mono">{{ rollbackTarget.previous_release_id }}</code> 重新指向
        <code class="mono">current</code>，并重启服务。原记录的状态不会被改写。
      </p>

      <h3 class="sub-title">目标服务器</h3>
      <div class="target-list">
        <label v-for="server in servers" :key="server.id" class="target-item">
          <input
            type="checkbox"
            :checked="rollbackTargets.has(server.id)"
            @change="rollbackTargets = toggleTarget(rollbackTargets, server.id)"
          />
          <span class="mono">{{ server.name }}</span>
          <span class="muted">{{ server.hostname }}:{{ server.port }}</span>
        </label>
      </div>

      <label class="field">
        <span class="field-label">原因</span>
        <input v-model="rollbackReason" class="field-input" placeholder="健康检查不过" />
      </label>

      <label class="confirm">
        <input v-model="rollbackConfirmed" type="checkbox" />
        <span>我确认要重启线上服务，并把版本切回 {{ rollbackTarget.previous_release_id }}</span>
      </label>

      <div class="form-actions">
        <button type="button" class="btn" :disabled="busy" @click="rollbackTarget = null">
          取消
        </button>
        <button
          type="button"
          class="btn btn-danger"
          :disabled="busy || !rollbackConfirmed || rollbackTargets.size === 0"
          @click="void submitRollback()"
        >
          确认回滚
        </button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.deps-title {
  margin: 0 0 4px;
  font-size: 20px;
  font-weight: 600;
}

.deps-hint {
  margin: 0;
  color: #909399;
}

.deps-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}

.filters {
  display: flex;
  gap: 16px;
  margin-bottom: 16px;
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

.deps-empty {
  padding: 32px;
  color: #909399;
  text-align: center;
  background: #fff;
  border: 1px solid var(--opspilot-border);
  border-radius: 8px;
}

.deps-table {
  width: 100%;
  background: #fff;
  border: 1px solid var(--opspilot-border);
  border-collapse: collapse;
  border-radius: 8px;
}

.deps-table th,
.deps-table td {
  padding: 12px 14px;
  border-bottom: 1px solid var(--opspilot-border);
  font-size: 13px;
  text-align: left;
  vertical-align: top;
}

.deps-table th {
  color: #909399;
  font-weight: 500;
  background: #fafafa;
}

.deps-table tr:last-child td {
  border-bottom: none;
}

.panel {
  margin-bottom: 16px;
  padding: 16px;
  background: #fff;
  border: 1px solid var(--opspilot-border);
  border-radius: 8px;
}

.panel-danger {
  border-color: #fbc4c4;
  background: #fffbfb;
}

.panel-title {
  margin: 0 0 8px;
  font-size: 15px;
  font-weight: 600;
}

.panel-hint {
  margin: 0 0 14px;
  color: #909399;
  font-size: 12px;
  line-height: 1.6;
}

.sub-title {
  margin: 16px 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: #606266;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 12px 16px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.field-label {
  color: #606266;
  font-size: 12px;
}

.field-input {
  padding: 8px 10px;
  border: 1px solid var(--opspilot-border);
  border-radius: 4px;
  font-size: 13px;
  background: #fff;
}

.target-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
}

.target-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}

.config-row {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}

.config-row .field-input {
  flex: 1;
}

.confirm {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin: 14px 0 0;
  font-size: 13px;
  color: #606266;
}

.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 14px;
}

.btn {
  padding: 8px 14px;
  border: 1px solid var(--opspilot-border);
  border-radius: 4px;
  background: #fff;
  color: #606266;
  font-size: 13px;
  cursor: pointer;
}

.btn:hover:not(:disabled) {
  border-color: #c6e2ff;
  color: #409eff;
}

.btn:disabled {
  opacity: 0.55;
  cursor: default;
}

.btn-primary {
  border-color: #409eff;
  background: #409eff;
  color: #fff;
}

.btn-primary:hover:not(:disabled) {
  border-color: #66b1ff;
  background: #66b1ff;
  color: #fff;
}

.btn-danger {
  border-color: #f56c6c;
  background: #f56c6c;
  color: #fff;
}

.btn-danger:hover:not(:disabled) {
  border-color: #f78989;
  background: #f78989;
  color: #fff;
}

.btn-small {
  padding: 4px 10px;
  font-size: 12px;
}

.row-actions {
  white-space: nowrap;
}

.kind {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
  color: #409eff;
  background: #ecf5ff;
}

.kind.undo {
  color: #e6a23c;
  background: #fdf6ec;
}

.error-detail {
  max-width: 260px;
  margin-top: 4px;
  color: #f56c6c;
  font-size: 12px;
  word-break: break-word;
}

.task-link {
  color: #409eff;
  text-decoration: none;
}

.task-link:hover {
  text-decoration: underline;
}

.muted {
  color: #909399;
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

.pill-succeeded {
  color: #67c23a;
  background: #f0f9eb;
}

.pill-rolled-back {
  color: #e6a23c;
  background: #fdf6ec;
}

.pill-failed {
  color: #f56c6c;
  background: #fef0f0;
}

.pill-cancelled {
  color: #909399;
  background: #f4f4f5;
}

.pill-running {
  color: #409eff;
  background: #ecf5ff;
}

.pill-pending {
  color: #909399;
  background: #f4f4f5;
}
</style>
