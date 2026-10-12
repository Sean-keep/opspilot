<script setup lang="ts">
/**
 * S1 server inventory: register a host, establish host-key trust, run the
 * read-only environment check, and look at the collected system facts.
 *
 * The host-key flow is deliberately two steps. Discovering a fingerprint only
 * records what the host offered; nothing runs against it until an operator
 * confirms that exact value. Hiding the button would not be a control — the
 * backend refuses the same way — but showing the fingerprint is how a human
 * gets a chance to notice a machine-in-the-middle.
 */
import { computed, onMounted, ref } from 'vue'

import { ApiError, NetworkError } from '@/api/client'
import {
  acceptHostKey,
  checkServer,
  createServer,
  deleteServer,
  discoverHostKey,
  listCredentials,
  listServers,
} from '@/api/inventory'
import PermissionGate from '@/components/PermissionGate.vue'
import RequestError from '@/components/RequestError.vue'
import type { CredentialOut, HostKeyDiscoverOut, ServerOut } from '@/types/api'

const servers = ref<ServerOut[]>([])
const credentials = ref<CredentialOut[]>([])
const loading = ref(false)
const loadError = ref<unknown>(null)

// Per-row busy state so a slow check does not lock the whole page.
const busy = ref<Record<number, string>>({})
const rowMessage = ref<Record<number, { kind: 'ok' | 'bad'; text: string }>>({})

// -- add form ---------------------------------------------------------------
const showAdd = ref(false)
const saving = ref(false)
const addError = ref<string | null>(null)
const form = ref({
  name: '',
  hostname: '',
  port: 22,
  ssh_user: 'opstest',
  credential_id: null as number | null,
  description: '',
})

// -- host-key confirmation --------------------------------------------------
const pendingHostKey = ref<{ server: ServerOut; offered: HostKeyDiscoverOut } | null>(null)
const confirmFingerprint = ref('')
const confirming = ref(false)
const confirmError = ref<string | null>(null)

const activeCredentials = computed(() =>
  credentials.value.filter((c) => c.status === 'active' && c.kind === 'ssh_key'),
)

function messageOf(cause: unknown): string {
  if (cause instanceof ApiError || cause instanceof NetworkError) {
    return cause.message
  }
  return '操作失败'
}

function setRow(id: number, kind: 'ok' | 'bad', text: string): void {
  rowMessage.value = { ...rowMessage.value, [id]: { kind, text } }
}

function hasRowMessage(id: number): boolean {
  return rowMessage.value[id] !== undefined
}

function rowMessageClass(id: number): string {
  return rowMessage.value[id]?.kind ?? ''
}

function rowMessageText(id: number): string {
  return rowMessage.value[id]?.text ?? ''
}

function clearRow(id: number): void {
  const next = { ...rowMessage.value }
  delete next[id]
  rowMessage.value = next
}

async function load(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    const [rows, creds] = await Promise.all([listServers(), listCredentials()])
    servers.value = rows
    credentials.value = creds
  } catch (cause) {
    loadError.value = cause
  } finally {
    loading.value = false
  }
}

async function submitAdd(): Promise<void> {
  saving.value = true
  addError.value = null
  try {
    await createServer({
      name: form.value.name.trim(),
      hostname: form.value.hostname.trim(),
      port: form.value.port,
      ssh_user: form.value.ssh_user.trim(),
      credential_id: form.value.credential_id,
      description: form.value.description.trim(),
    })
    showAdd.value = false
    form.value = {
      name: '',
      hostname: '',
      port: 22,
      ssh_user: 'opstest',
      credential_id: null,
      description: '',
    }
    await load()
  } catch (cause) {
    addError.value = messageOf(cause)
  } finally {
    saving.value = false
  }
}

async function runCheck(server: ServerOut): Promise<void> {
  busy.value = { ...busy.value, [server.id]: 'check' }
  clearRow(server.id)
  try {
    const result = await checkServer(server.id)
    if (result.ok) {
      const f = result.facts
      setRow(
        server.id,
        'ok',
        f
          ? `检查通过：${f.os_name} ${f.os_version} · ${f.cpu_count} 核 · 内存 ${f.memory_total_mb} MB`
          : '检查通过',
      )
    } else {
      setRow(server.id, 'bad', `检查失败：${result.detail}`)
    }
    await load()
  } catch (cause) {
    setRow(server.id, 'bad', messageOf(cause))
  } finally {
    const next = { ...busy.value }
    delete next[server.id]
    busy.value = next
  }
}

async function startHostKey(server: ServerOut): Promise<void> {
  busy.value = { ...busy.value, [server.id]: 'discover' }
  clearRow(server.id)
  try {
    const offered = await discoverHostKey(server.id)
    pendingHostKey.value = { server, offered }
    confirmFingerprint.value = ''
    confirmError.value = null
    await load()
  } catch (cause) {
    setRow(server.id, 'bad', messageOf(cause))
  } finally {
    const next = { ...busy.value }
    delete next[server.id]
    busy.value = next
  }
}

async function confirmHostKey(): Promise<void> {
  const pending = pendingHostKey.value
  if (pending === null) {
    return
  }
  confirming.value = true
  confirmError.value = null
  try {
    await acceptHostKey(pending.server.id, confirmFingerprint.value.trim())
    pendingHostKey.value = null
    setRow(pending.server.id, 'ok', '主机密钥已确认并固定')
    await load()
  } catch (cause) {
    confirmError.value = messageOf(cause)
  } finally {
    confirming.value = false
  }
}

async function removeServer(server: ServerOut): Promise<void> {
  if (!window.confirm(`确定停用服务器「${server.name}」？记录会保留以供审计。`)) {
    return
  }
  busy.value = { ...busy.value, [server.id]: 'delete' }
  try {
    await deleteServer(server.id)
    await load()
  } catch (cause) {
    setRow(server.id, 'bad', messageOf(cause))
  } finally {
    const next = { ...busy.value }
    delete next[server.id]
    busy.value = next
  }
}

function hostKeyLabel(state: string): string {
  switch (state) {
    case 'verified':
      return '已确认'
    case 'pending_confirm':
      return '待确认'
    case 'mismatch':
      return '指纹不一致'
    default:
      return '未确认'
  }
}

function checkLabel(server: ServerOut): string {
  if (server.last_check_at === null) {
    return '未检查'
  }
  return server.last_check_status === 'ok' ? '正常' : '失败'
}

function factsSummary(server: ServerOut): string {
  const f = server.latest_facts
  if (f === null) {
    return '—'
  }
  return [
    `${f.os_name} ${f.os_version}`,
    `${f.cpu_count} 核`,
    `内存 ${f.memory_total_mb} MB`,
    `磁盘 ${f.disk_available_gb}/${f.disk_total_gb} GB`,
  ].join(' · ')
}

function formatTime(value: string | null): string {
  if (value === null || value === '') {
    return '—'
  }
  return new Date(value).toLocaleString()
}

onMounted(() => void load())
</script>

<template>
  <section class="servers">
    <header class="servers-header">
      <div>
        <h1 class="servers-title">服务器</h1>
        <p class="servers-hint">
          添加主机后需要先确认 SSH 主机密钥指纹，之后才能执行连通性检查。检查只读，不会修改目标机。
        </p>
      </div>
      <PermissionGate permission="server.create">
        <button class="btn btn-primary" type="button" @click="showAdd = !showAdd">
          {{ showAdd ? '取消' : '添加服务器' }}
        </button>
      </PermissionGate>
    </header>

    <RequestError v-if="loadError !== null" :error="loadError" />

    <form v-if="showAdd" class="add-form" @submit.prevent="submitAdd">
      <h2 class="add-form-title">添加服务器</h2>
      <div class="add-form-grid">
        <label class="field">
          <span class="field-label">名称</span>
          <input v-model="form.name" class="field-input" required maxlength="128" />
        </label>
        <label class="field">
          <span class="field-label">主机 / IP</span>
          <input
            v-model="form.hostname"
            class="field-input"
            required
            maxlength="255"
            placeholder="10.0.0.11"
          />
        </label>
        <label class="field">
          <span class="field-label">端口</span>
          <input
            v-model.number="form.port"
            class="field-input"
            type="number"
            min="1"
            max="65535"
            required
          />
        </label>
        <label class="field">
          <span class="field-label">SSH 用户</span>
          <input v-model="form.ssh_user" class="field-input" required maxlength="64" />
        </label>
        <label class="field">
          <span class="field-label">凭据</span>
          <select v-model="form.credential_id" class="field-input">
            <option :value="null">不使用凭据</option>
            <option v-for="c in activeCredentials" :key="c.id" :value="c.id">
              {{ c.name }}（{{ c.kind }}）
            </option>
          </select>
        </label>
        <label class="field field-wide">
          <span class="field-label">备注</span>
          <input v-model="form.description" class="field-input" maxlength="4000" />
        </label>
      </div>
      <p v-if="addError" class="form-error" role="alert">{{ addError }}</p>
      <div class="add-form-actions">
        <button class="btn btn-primary" type="submit" :disabled="saving">
          {{ saving ? '保存中…' : '保存' }}
        </button>
      </div>
    </form>

    <div v-if="loading && servers.length === 0" class="servers-empty">加载中…</div>
    <div v-else-if="servers.length === 0" class="servers-empty">
      还没有服务器。先添加一台，再确认它的主机密钥。
    </div>

    <table v-else class="servers-table">
      <thead>
        <tr>
          <th>名称</th>
          <th>地址</th>
          <th>主机密钥</th>
          <th>最后检查</th>
          <th>系统事实</th>
          <th class="col-actions">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="server in servers" :key="server.id">
          <td>
            <div class="server-name">{{ server.name }}</div>
            <div class="server-sub">{{ server.ssh_user || '—' }}</div>
          </td>
          <td>
            <div>{{ server.hostname }}:{{ server.port }}</div>
            <div class="server-sub">{{ server.status === 'active' ? '启用' : '停用' }}</div>
          </td>
          <td>
            <span class="pill" :class="`pill-${server.host_key_state}`">
              {{ hostKeyLabel(server.host_key_state) }}
            </span>
            <div v-if="server.host_key_fingerprint" class="server-sub mono">
              {{ server.host_key_fingerprint }}
            </div>
          </td>
          <td>
            <span class="pill" :class="server.last_check_status === 'ok' ? 'pill-ok' : 'pill-unknown'">
              {{ checkLabel(server) }}
            </span>
            <div class="server-sub">{{ formatTime(server.last_check_at) }}</div>
          </td>
          <td class="facts-cell">
            {{ factsSummary(server) }}
            <div v-if="server.latest_facts" class="server-sub">
              CPU：{{ server.latest_facts.cpu_model || '—' }}
            </div>
          </td>
          <td class="col-actions">
            <div class="row-actions">
              <PermissionGate permission="server.check">
                <button
                  class="btn btn-small"
                  type="button"
                  :disabled="busy[server.id] !== undefined"
                  @click="runCheck(server)"
                >
                  {{ busy[server.id] === 'check' ? '检查中…' : '检查' }}
                </button>
              </PermissionGate>
              <PermissionGate permission="server.check">
                <button
                  class="btn btn-small"
                  type="button"
                  :disabled="busy[server.id] !== undefined"
                  @click="startHostKey(server)"
                >
                  {{ busy[server.id] === 'discover' ? '读取中…' : '主机密钥' }}
                </button>
              </PermissionGate>
              <PermissionGate permission="server.delete">
                <button
                  class="btn btn-small btn-danger"
                  type="button"
                  :disabled="busy[server.id] !== undefined"
                  @click="removeServer(server)"
                >
                  停用
                </button>
              </PermissionGate>
            </div>
            <p
              v-if="hasRowMessage(server.id)"
              class="row-message"
              :class="rowMessageClass(server.id)"
            >
              {{ rowMessageText(server.id) }}
            </p>
          </td>
        </tr>
      </tbody>
    </table>

    <!-- Host-key confirmation: the fingerprint must be typed, not just clicked. -->
    <div v-if="pendingHostKey" class="modal-backdrop" role="presentation">
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="hostkey-title">
        <h2 id="hostkey-title" class="modal-title">确认主机密钥</h2>
        <p class="modal-body">
          主机 <strong>{{ pendingHostKey.server.name }}</strong>
          （{{ pendingHostKey.server.hostname }}:{{ pendingHostKey.server.port }}）提供了以下密钥指纹。
          请与目标机上 <code>ssh-keygen -lf /etc/ssh/ssh_host_*_key.pub</code> 的输出核对后再确认。
        </p>
        <dl class="modal-facts">
          <dt>算法</dt>
          <dd>{{ pendingHostKey.offered.algorithm }}</dd>
          <dt>指纹</dt>
          <dd class="mono">{{ pendingHostKey.offered.fingerprint }}</dd>
        </dl>
        <label class="field">
          <span class="field-label">请逐字输入指纹以确认</span>
          <input
            v-model="confirmFingerprint"
            class="field-input mono"
            :placeholder="pendingHostKey.offered.fingerprint"
            autocomplete="off"
          />
        </label>
        <p v-if="confirmError" class="form-error" role="alert">{{ confirmError }}</p>
        <div class="modal-actions">
          <button class="btn" type="button" @click="pendingHostKey = null">取消</button>
          <button
            class="btn btn-primary"
            type="button"
            :disabled="confirming || confirmFingerprint.trim() === ''"
            @click="confirmHostKey"
          >
            {{ confirming ? '确认中…' : '确认并固定' }}
          </button>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.servers-title {
  margin: 0 0 4px;
  font-size: 20px;
  font-weight: 600;
}

.servers-hint {
  margin: 0;
  color: #909399;
}

.servers-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 20px;
}

.servers-empty {
  padding: 32px;
  color: #909399;
  text-align: center;
  background: #fff;
  border: 1px solid var(--opspilot-border);
  border-radius: 8px;
}

.servers-table {
  width: 100%;
  background: #fff;
  border: 1px solid var(--opspilot-border);
  border-collapse: collapse;
  border-radius: 8px;
}

.servers-table th,
.servers-table td {
  padding: 12px 14px;
  border-bottom: 1px solid var(--opspilot-border);
  font-size: 13px;
  text-align: left;
  vertical-align: top;
}

.servers-table th {
  color: #909399;
  font-weight: 500;
  background: #fafafa;
}

.servers-table tr:last-child td {
  border-bottom: none;
}

.server-name {
  font-weight: 600;
}

.server-sub {
  margin-top: 2px;
  color: #909399;
  font-size: 12px;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  word-break: break-all;
}

.facts-cell {
  max-width: 280px;
}

.col-actions {
  width: 220px;
}

.row-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.row-message {
  margin: 6px 0 0;
  font-size: 12px;
}

.row-message.ok {
  color: #67c23a;
}

.row-message.bad {
  color: #f56c6c;
}

.pill {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
}

.pill-verified,
.pill-ok {
  color: #67c23a;
  background: #f0f9eb;
}

.pill-pending_confirm {
  color: #e6a23c;
  background: #fdf6ec;
}

.pill-mismatch {
  color: #f56c6c;
  background: #fef0f0;
}

.pill-unverified,
.pill-unknown,
.pill-disabled {
  color: #909399;
  background: #f4f4f5;
}

/* -- form ---------------------------------------------------------------- */
.add-form {
  margin-bottom: 20px;
  padding: 20px;
  background: #fff;
  border: 1px solid var(--opspilot-border);
  border-radius: 8px;
}

.add-form-title {
  margin: 0 0 16px;
  font-size: 15px;
  font-weight: 600;
}

.add-form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 14px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.field-wide {
  grid-column: 1 / -1;
}

.field-label {
  color: #606266;
  font-size: 13px;
}

.field-input {
  padding: 8px 10px;
  border: 1px solid var(--opspilot-border);
  border-radius: 4px;
  font-size: 13px;
}

.add-form-actions {
  margin-top: 16px;
}

.form-error {
  margin: 12px 0 0;
  color: #f56c6c;
}

/* -- buttons ------------------------------------------------------------- */
.btn {
  padding: 8px 14px;
  border: 1px solid var(--opspilot-border);
  border-radius: 4px;
  background: #fff;
  color: #606266;
  font-size: 13px;
  cursor: pointer;
}

.btn:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.btn-primary {
  border-color: #409eff;
  background: #409eff;
  color: #fff;
}

.btn-danger {
  border-color: #f56c6c;
  color: #f56c6c;
}

.btn-small {
  padding: 4px 10px;
  font-size: 12px;
}

/* -- modal --------------------------------------------------------------- */
.modal-backdrop {
  position: fixed;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.35);
}

.modal {
  width: min(560px, calc(100vw - 32px));
  padding: 24px;
  background: #fff;
  border-radius: 8px;
}

.modal-title {
  margin: 0 0 12px;
  font-size: 16px;
  font-weight: 600;
}

.modal-body {
  margin: 0 0 16px;
  color: #606266;
  line-height: 1.6;
}

.modal-facts {
  display: grid;
  grid-template-columns: 64px 1fr;
  gap: 6px 12px;
  margin: 0 0 16px;
  font-size: 13px;
}

.modal-facts dt {
  color: #909399;
}

.modal-facts dd {
  margin: 0;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
}
</style>
