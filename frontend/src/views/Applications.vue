<script setup lang="ts">
/**
 * S4 applications and their environments.
 *
 * This page is definitions only: names, run users, service units, health
 * checks. Nothing here runs anything on a host — the first command is issued
 * when someone submits a release from the deployments page. That split is the
 * point: describing where a service lives should never be enough to touch it.
 *
 * `config_ref` is displayed as the opaque reference it is. Secret values are
 * resolved by the worker at execution time and are never returned here.
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import {
  createApplication,
  createEnvironment,
  listApplications,
  updateEnvironment,
} from '@/api/applications'
import PermissionGate from '@/components/PermissionGate.vue'
import RequestError from '@/components/RequestError.vue'
import { useAuthStore } from '@/stores/auth'
import type {
  AppEnvironmentName,
  AppEnvironmentOut,
  ApplicationOut,
} from '@/types/api'

const router = useRouter()
const auth = useAuthStore()

const apps = ref<ApplicationOut[]>([])
const loading = ref(false)
const loadError = ref<unknown>(null)
const actionError = ref<unknown>(null)
const busy = ref(false)

/** Which application rows are open. Closed by default — the list is a list. */
const openAppIds = ref<Set<number>>(new Set())

// -- new application -------------------------------------------------------
const showAppForm = ref(false)
const appName = ref('')
const appRuntime = ref('python')
const appRunUser = ref('')
const appRepoUrl = ref('')
const appBranch = ref('')
const appDescription = ref('')

// -- new / edit environment ------------------------------------------------
const envFormAppId = ref<number | null>(null)
const envFormEditId = ref<number | null>(null)
const envName = ref<AppEnvironmentName>('test')
const envConfigRef = ref('')
const envServiceName = ref('')
const envUnitPath = ref('')
const envHealthUrl = ref('')
const envHealthTimeout = ref(10)
const envKeepReleases = ref(5)

const ENVIRONMENT_OPTIONS: { value: AppEnvironmentName; label: string }[] = [
  { value: 'test', label: 'test' },
  { value: 'staging', label: 'staging' },
  { value: 'production', label: 'production' },
]

const canEdit = computed(() => auth.hasPermission('app.update'))
const canCreate = computed(() => auth.hasPermission('app.create'))

function toggleApp(id: number): void {
  const next = new Set(openAppIds.value)
  if (next.has(id)) {
    next.delete(id)
  } else {
    next.add(id)
  }
  openAppIds.value = next
}

function isOpen(id: number): boolean {
  return openAppIds.value.has(id)
}

async function load(): Promise<void> {
  loading.value = true
  try {
    apps.value = await listApplications()
    loadError.value = null
  } catch (cause) {
    loadError.value = cause
  } finally {
    loading.value = false
  }
}

function resetAppForm(): void {
  appName.value = ''
  appRuntime.value = 'python'
  appRunUser.value = ''
  appRepoUrl.value = ''
  appBranch.value = ''
  appDescription.value = ''
}

function openAppForm(): void {
  resetAppForm()
  actionError.value = null
  showAppForm.value = true
}

async function submitApp(): Promise<void> {
  busy.value = true
  actionError.value = null
  try {
    const body: Parameters<typeof createApplication>[0] = {
      name: appName.value.trim(),
      runtime_type: appRuntime.value as 'python',
    }
    const runUser = appRunUser.value.trim()
    if (runUser !== '') body.run_user = runUser
    const repoUrl = appRepoUrl.value.trim()
    if (repoUrl !== '') body.repo_url = repoUrl
    const branch = appBranch.value.trim()
    if (branch !== '') body.default_branch = branch
    const description = appDescription.value.trim()
    if (description !== '') body.description = description

    const created = await createApplication(body)
    showAppForm.value = false
    resetAppForm()
    const next = new Set(openAppIds.value)
    next.add(created.id)
    openAppIds.value = next
    await load()
  } catch (cause) {
    actionError.value = cause
  } finally {
    busy.value = false
  }
}

function unusedEnvironmentNames(app: ApplicationOut): AppEnvironmentName[] {
  const used = new Set(app.environments.map((e) => e.name))
  return ENVIRONMENT_OPTIONS.map((o) => o.value).filter((name) => !used.has(name))
}

function openEnvForm(app: ApplicationOut, existing?: AppEnvironmentOut): void {
  const available = unusedEnvironmentNames(app)
  envFormAppId.value = app.id
  envFormEditId.value = existing === undefined ? null : existing.id
  envName.value = existing === undefined ? (available[0] ?? 'test') : existing.name
  envConfigRef.value = existing === undefined ? '' : existing.config_ref
  envServiceName.value = existing === undefined ? '' : existing.service_name
  envUnitPath.value = existing === undefined ? '' : existing.unit_path
  envHealthUrl.value = existing === undefined ? '' : existing.health_url
  envHealthTimeout.value = existing === undefined ? 10 : existing.health_timeout_s
  envKeepReleases.value = existing === undefined ? 5 : existing.keep_releases
  actionError.value = null
}

function closeEnvForm(): void {
  envFormAppId.value = null
  envFormEditId.value = null
}

async function submitEnv(): Promise<void> {
  const appId = envFormAppId.value
  if (appId === null) return
  busy.value = true
  actionError.value = null
  try {
    const shared = {
      config_ref: envConfigRef.value.trim(),
      service_name: envServiceName.value.trim(),
      unit_path: envUnitPath.value.trim(),
      health_url: envHealthUrl.value.trim(),
      health_timeout_s: envHealthTimeout.value,
      keep_releases: envKeepReleases.value,
    }
    if (envFormEditId.value === null) {
      await createEnvironment(appId, { name: envName.value, ...shared })
    } else {
      await updateEnvironment(appId, envFormEditId.value, shared)
    }
    closeEnvForm()
    await load()
  } catch (cause) {
    actionError.value = cause
  } finally {
    busy.value = false
  }
}

/** Send the operator to the deploy form with the application already chosen. */
function goToDeploy(app: ApplicationOut, env?: AppEnvironmentOut): void {
  const query: Record<string, string> = { app: String(app.id) }
  if (env !== undefined) {
    query.env = env.name
  }
  void router.push({ name: 'deployments', query })
}

function formatRef(value: string): string {
  return value === '' ? '—' : value
}

onMounted(() => {
  void load()
})
</script>

<template>
  <section class="apps">
    <header class="apps-header">
      <div>
        <h1 class="apps-title">应用</h1>
        <p class="apps-hint">
          一个应用是「跑什么、以谁的身份跑、放在哪台机器的哪个服务单元里」的定义。
          真正下发安装命令发生在发布记录页。
        </p>
      </div>
      <PermissionGate permission="app.create">
        <button type="button" class="btn btn-primary" @click="openAppForm">新建应用</button>
      </PermissionGate>
    </header>

    <RequestError v-if="loadError !== null" :error="loadError" />
    <RequestError v-if="actionError !== null" :error="actionError" />

    <div v-if="showAppForm" class="panel">
      <h2 class="panel-title">新建应用</h2>
      <div class="form-grid">
        <label class="field">
          <span class="field-label">名称</span>
          <input v-model="appName" class="field-input mono" placeholder="billing-api" />
          <span class="field-hint">小写字母开头，只能含小写字母、数字和短横线；会成为目录名</span>
        </label>
        <label class="field">
          <span class="field-label">运行时</span>
          <select v-model="appRuntime" class="field-input">
            <option value="python">python</option>
          </select>
        </label>
        <label class="field">
          <span class="field-label">运行用户</span>
          <input v-model="appRunUser" class="field-input mono" placeholder="billing" />
          <span class="field-hint">非 root；留空表示部署时再决定</span>
        </label>
        <label class="field">
          <span class="field-label">仓库地址</span>
          <input v-model="appRepoUrl" class="field-input mono" placeholder="https://…" />
        </label>
        <label class="field">
          <span class="field-label">默认分支</span>
          <input v-model="appBranch" class="field-input mono" placeholder="main" />
        </label>
        <label class="field">
          <span class="field-label">描述</span>
          <input v-model="appDescription" class="field-input" placeholder="计费对外 API" />
        </label>
      </div>
      <div class="form-actions">
        <button type="button" class="btn" :disabled="busy" @click="showAppForm = false">取消</button>
        <button type="button" class="btn btn-primary" :disabled="busy" @click="void submitApp()">
          创建
        </button>
      </div>
    </div>

    <div v-if="loading && apps.length === 0" class="apps-empty">加载中…</div>
    <div v-else-if="apps.length === 0" class="apps-empty">
      还没有应用。点「新建应用」定义第一个，然后给它加一个环境。
    </div>

    <table v-else class="apps-table">
      <thead>
        <tr>
          <th>名称</th>
          <th>运行时</th>
          <th>运行用户</th>
          <th>环境</th>
          <th>描述</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <template v-for="app in apps" :key="app.id">
          <tr class="app-row" :class="{ open: isOpen(app.id) }">
            <td>
              <button type="button" class="app-toggle" @click="toggleApp(app.id)">
                <span class="app-caret" aria-hidden="true">{{ isOpen(app.id) ? '▾' : '▸' }}</span>
                <span class="mono app-name">{{ app.name }}</span>
              </button>
            </td>
            <td class="mono">{{ app.runtime_type }}</td>
            <td class="mono">{{ app.run_user || '—' }}</td>
            <td>
              <span v-for="env in app.environments" :key="env.id" class="env-chip">{{ env.name }}</span>
              <span v-if="app.environments.length === 0" class="muted">未配置</span>
            </td>
            <td>{{ app.description || '—' }}</td>
            <td class="row-actions">
              <button type="button" class="btn btn-small" @click="goToDeploy(app)">发布</button>
            </td>
          </tr>

          <tr v-if="isOpen(app.id)" class="app-detail">
            <td colspan="6">
              <h3 class="detail-title">环境</h3>
              <table v-if="app.environments.length > 0" class="env-table">
                <thead>
                  <tr>
                    <th>名称</th>
                    <th>服务单元</th>
                    <th>健康检查</th>
                    <th>配置引用</th>
                    <th>保留版本</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="env in app.environments" :key="env.id">
                    <td class="mono">{{ env.name }}</td>
                    <td class="mono">
                      <div>{{ env.service_name || '—' }}</div>
                      <div v-if="env.unit_path" class="muted mono">{{ env.unit_path }}</div>
                    </td>
                    <td class="mono">
                      {{ env.health_url || '—' }}
                      <div v-if="env.health_url" class="muted">{{ env.health_timeout_s }}s</div>
                    </td>
                    <td class="mono">{{ formatRef(env.config_ref) }}</td>
                    <td>{{ env.keep_releases }}</td>
                    <td class="row-actions">
                      <button type="button" class="btn btn-small" @click="goToDeploy(app, env)">
                        发布
                      </button>
                      <PermissionGate permission="app.update">
                        <button
                          type="button"
                          class="btn btn-small"
                          @click="openEnvForm(app, env)"
                        >
                          编辑
                        </button>
                      </PermissionGate>
                    </td>
                  </tr>
                </tbody>
              </table>
              <p v-else class="muted">
                这个应用还没有环境。至少要有一个，才能把版本发出去。
              </p>

              <PermissionGate permission="app.update">
                <button
                  v-if="canEdit && unusedEnvironmentNames(app).length > 0"
                  type="button"
                  class="btn btn-small"
                  @click="openEnvForm(app)"
                >
                  添加环境
                </button>
              </PermissionGate>

              <div v-if="envFormAppId === app.id" class="panel panel-nested">
                <h4 class="panel-title">
                  {{ envFormEditId === null ? '添加环境' : '编辑环境' }}
                </h4>
                <div class="form-grid">
                  <label v-if="envFormEditId === null" class="field">
                    <span class="field-label">名称</span>
                    <select v-model="envName" class="field-input">
                      <option v-for="name in unusedEnvironmentNames(app)" :key="name" :value="name">
                        {{ name }}
                      </option>
                    </select>
                  </label>
                  <label class="field">
                    <span class="field-label">服务单元名</span>
                    <input
                      v-model="envServiceName"
                      class="field-input mono"
                      placeholder="opspilot-billing"
                    />
                    <span class="field-hint">必须匹配 opspilot-* 白名单</span>
                  </label>
                  <label class="field">
                    <span class="field-label">unit 路径</span>
                    <input
                      v-model="envUnitPath"
                      class="field-input mono"
                      placeholder="/etc/systemd/system/opspilot-billing.service"
                    />
                  </label>
                  <label class="field">
                    <span class="field-label">健康检查 URL</span>
                    <input
                      v-model="envHealthUrl"
                      class="field-input mono"
                      placeholder="http://127.0.0.1:8000/healthz"
                    />
                  </label>
                  <label class="field">
                    <span class="field-label">健康检查超时（秒）</span>
                    <input
                      v-model.number="envHealthTimeout"
                      type="number"
                      min="1"
                      max="60"
                      class="field-input"
                    />
                  </label>
                  <label class="field">
                    <span class="field-label">配置引用</span>
                    <input v-model="envConfigRef" class="field-input mono" placeholder="cred:12" />
                    <span class="field-hint">
                      只填引用，不填密文；worker 在执行时才解析出值
                    </span>
                  </label>
                  <label class="field">
                    <span class="field-label">保留旧版本数</span>
                    <input
                      v-model.number="envKeepReleases"
                      type="number"
                      min="1"
                      max="100"
                      class="field-input"
                    />
                    <span class="field-hint">shared/data 永不自动清理</span>
                  </label>
                </div>
                <div class="form-actions">
                  <button type="button" class="btn" :disabled="busy" @click="closeEnvForm">
                    取消
                  </button>
                  <button
                    type="button"
                    class="btn btn-primary"
                    :disabled="busy"
                    @click="void submitEnv()"
                  >
                    保存
                  </button>
                </div>
              </div>
            </td>
          </tr>
        </template>
      </tbody>
    </table>

    <p v-if="!canCreate && !canEdit" class="muted apps-foot">
      当前账号只有读权限。后端同样会拒绝任何写操作——隐藏按钮不是权限边界。
    </p>
  </section>
</template>

<style scoped>
.apps-title {
  margin: 0 0 4px;
  font-size: 20px;
  font-weight: 600;
}

.apps-hint {
  margin: 0;
  color: #909399;
}

.apps-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 20px;
}

.apps-empty {
  padding: 32px;
  color: #909399;
  text-align: center;
  background: #fff;
  border: 1px solid var(--opspilot-border);
  border-radius: 8px;
}

.apps-table {
  width: 100%;
  background: #fff;
  border: 1px solid var(--opspilot-border);
  border-collapse: collapse;
  border-radius: 8px;
}

.apps-table th,
.apps-table td {
  padding: 12px 14px;
  border-bottom: 1px solid var(--opspilot-border);
  font-size: 13px;
  text-align: left;
  vertical-align: top;
}

.apps-table th {
  color: #909399;
  font-weight: 500;
  background: #fafafa;
}

.app-row.open {
  background: #fafcff;
}

.app-toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 0;
  border: none;
  background: none;
  cursor: pointer;
  font: inherit;
  color: inherit;
}

.app-caret {
  color: #909399;
}

.app-name {
  color: #409eff;
  font-weight: 600;
}

.app-detail > td {
  background: #fafafa;
}

.detail-title {
  margin: 0 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: #606266;
}

.env-table {
  width: 100%;
  margin-bottom: 12px;
  background: #fff;
  border: 1px solid var(--opspilot-border);
  border-collapse: collapse;
}

.env-table th,
.env-table td {
  padding: 8px 12px;
  border-bottom: 1px solid var(--opspilot-border);
  font-size: 12px;
  text-align: left;
  vertical-align: top;
}

.env-table th {
  color: #909399;
  font-weight: 500;
}

.env-chip {
  display: inline-block;
  margin: 0 4px 2px 0;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
  color: #409eff;
  background: #ecf5ff;
}

.row-actions {
  white-space: nowrap;
}

.row-actions .btn {
  margin-left: 6px;
}

.panel {
  margin-bottom: 16px;
  padding: 16px;
  background: #fff;
  border: 1px solid var(--opspilot-border);
  border-radius: 8px;
}

.panel-nested {
  margin-top: 12px;
  background: #fff;
}

.panel-title {
  margin: 0 0 12px;
  font-size: 15px;
  font-weight: 600;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
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

.field-hint {
  color: #909399;
  font-size: 11px;
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

.btn-small {
  padding: 4px 10px;
  font-size: 12px;
}

.muted {
  color: #909399;
}

.apps-foot {
  margin-top: 16px;
  font-size: 12px;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  word-break: break-all;
}
</style>
