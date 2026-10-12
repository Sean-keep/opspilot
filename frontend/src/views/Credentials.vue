<script setup lang="ts">
/**
 * S1 credentials: store, rotate, and retire secrets.
 *
 * The plaintext is typed here once and never comes back. Every field below is
 * metadata — name, kind, a non-secret fingerprint, and the `secret_ref` that
 * templates and tasks use to refer to the material without holding it.
 */
import { onMounted, ref } from 'vue'

import { ApiError, NetworkError } from '@/api/client'
import {
  createCredential,
  listCredentials,
  retireCredential,
  rotateCredential,
} from '@/api/inventory'
import PermissionGate from '@/components/PermissionGate.vue'
import RequestError from '@/components/RequestError.vue'
import type { CredentialOut } from '@/types/api'

const credentials = ref<CredentialOut[]>([])
const loading = ref(false)
const loadError = ref<unknown>(null)
const showRetired = ref(false)

const busy = ref<Record<number, string>>({})
const rowMessage = ref<Record<number, { kind: 'ok' | 'bad'; text: string }>>({})

const showAdd = ref(false)
const saving = ref(false)
const addError = ref<unknown>(null)
const form = ref({
  name: '',
  kind: 'ssh_key' as CredentialOut['kind'],
  description: '',
  secret: '',
})

const rotating = ref<number | null>(null)
const rotateSecret = ref('')
const rotateError = ref<unknown>(null)

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

async function load(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    credentials.value = await listCredentials(showRetired.value)
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
    await createCredential({
      name: form.value.name.trim(),
      kind: form.value.kind,
      description: form.value.description.trim(),
      secret: form.value.secret,
    })
    showAdd.value = false
    // Drop the plaintext from component state as soon as it has been sent.
    form.value = { name: '', kind: 'ssh_key', description: '', secret: '' }
    await load()
  } catch (cause) {
    addError.value = cause
  } finally {
    saving.value = false
  }
}

function startRotate(row: CredentialOut): void {
  rotating.value = row.id
  rotateSecret.value = ''
  rotateError.value = null
}

async function submitRotate(id: number): Promise<void> {
  busy.value = { ...busy.value, [id]: 'rotate' }
  try {
    await rotateCredential(id, rotateSecret.value)
    rotating.value = null
    rotateSecret.value = ''
    setRow(id, 'ok', '已轮换；secret_ref 保持不变')
    await load()
  } catch (cause) {
    rotateError.value = cause
  } finally {
    const next = { ...busy.value }
    delete next[id]
    busy.value = next
  }
}

async function retire(row: CredentialOut): Promise<void> {
  if (!window.confirm(`确定停用凭据「${row.name}」？引用它的服务器需要改绑。`)) {
    return
  }
  busy.value = { ...busy.value, [row.id]: 'retire' }
  try {
    await retireCredential(row.id)
    setRow(row.id, 'ok', '已停用')
    await load()
  } catch (cause) {
    setRow(row.id, 'bad', messageOf(cause))
  } finally {
    const next = { ...busy.value }
    delete next[row.id]
    busy.value = next
  }
}

function kindLabel(kind: string): string {
  switch (kind) {
    case 'ssh_key':
      return 'SSH 私钥'
    case 'ssh_password':
      return 'SSH 密码'
    case 'sudo_password':
      return 'sudo 密码'
    default:
      return kind
  }
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
  <section class="credentials">
    <header class="credentials-header">
      <div>
        <h1 class="credentials-title">凭据</h1>
        <p class="credentials-hint">
          密钥写入后立即加密存储，界面永远不会回显。这里只显示元数据与非敏感指纹，任务与模板通过
          <code>secret_ref</code> 引用它们。
        </p>
      </div>
      <PermissionGate permission="credential.create">
        <button class="btn btn-primary" type="button" @click="showAdd = !showAdd">
          {{ showAdd ? '取消' : '添加凭据' }}
        </button>
      </PermissionGate>
    </header>

    <RequestError v-if="loadError !== null" :error="loadError" />

    <form v-if="showAdd" class="add-form" @submit.prevent="submitAdd">
      <h2 class="add-form-title">添加凭据</h2>
      <div class="add-form-grid">
        <label class="field">
          <span class="field-label">名称</span>
          <input v-model="form.name" class="field-input" required maxlength="128" />
        </label>
        <label class="field">
          <span class="field-label">类型</span>
          <select v-model="form.kind" class="field-input">
            <option value="ssh_key">SSH 私钥</option>
            <option value="ssh_password">SSH 密码</option>
            <option value="sudo_password">sudo 密码</option>
          </select>
        </label>
        <label class="field field-wide">
          <span class="field-label">备注</span>
          <input v-model="form.description" class="field-input" maxlength="4000" />
        </label>
        <label class="field field-wide">
          <span class="field-label">{{ form.kind === 'ssh_key' ? '私钥内容' : '密码' }}</span>
          <textarea
            v-model="form.secret"
            class="field-input field-secret"
            rows="5"
            required
            autocomplete="off"
            :placeholder="form.kind === 'ssh_key' ? '-----BEGIN OPENSSH PRIVATE KEY-----' : ''"
          />
          <span class="field-note">写入后不会再次显示，请自行妥善保管原件。</span>
        </label>
      </div>
      <RequestError v-if="addError !== null" :error="addError" />
      <div class="add-form-actions">
        <button class="btn btn-primary" type="submit" :disabled="saving">
          {{ saving ? '保存中…' : '保存' }}
        </button>
      </div>
    </form>

    <label class="toggle">
      <input v-model="showRetired" type="checkbox" @change="load" />
      <span>显示已停用</span>
    </label>

    <div v-if="loading && credentials.length === 0" class="credentials-empty">加载中…</div>
    <div v-else-if="credentials.length === 0" class="credentials-empty">
      还没有凭据。添加一把 SSH 私钥，才能对服务器做连通性检查。
    </div>

    <table v-else class="credentials-table">
      <thead>
        <tr>
          <th>名称</th>
          <th>类型</th>
          <th>引用</th>
          <th>指纹</th>
          <th>状态</th>
          <th>最近使用</th>
          <th class="col-actions">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in credentials" :key="row.id">
          <td>
            <div class="cred-name">{{ row.name }}</div>
            <div class="cred-sub">{{ row.description || '—' }}</div>
          </td>
          <td>{{ kindLabel(row.kind) }}</td>
          <td class="mono">{{ row.secret_ref }}</td>
          <td class="mono">{{ row.fingerprint }}</td>
          <td>
            <span class="pill" :class="row.status === 'active' ? 'pill-active' : 'pill-retired'">
              {{ row.status === 'active' ? '启用' : '已停用' }}
            </span>
          </td>
          <td>{{ formatTime(row.used_at) }}</td>
          <td class="col-actions">
            <div class="row-actions">
              <PermissionGate permission="credential.update">
                <button
                  class="btn btn-small"
                  type="button"
                  :disabled="busy[row.id] !== undefined"
                  @click="startRotate(row)"
                >
                  轮换
                </button>
              </PermissionGate>
              <PermissionGate permission="credential.delete">
                <button
                  class="btn btn-small btn-danger"
                  type="button"
                  :disabled="busy[row.id] !== undefined || row.status !== 'active'"
                  @click="retire(row)"
                >
                  停用
                </button>
              </PermissionGate>
            </div>
            <p
              v-if="hasRowMessage(row.id)"
              class="row-message"
              :class="rowMessageClass(row.id)"
            >
              {{ rowMessageText(row.id) }}
            </p>
            <form
              v-if="rotating === row.id"
              class="rotate-form"
              @submit.prevent="submitRotate(row.id)"
            >
              <input
                v-model="rotateSecret"
                class="field-input field-secret"
                type="password"
                autocomplete="off"
                placeholder="新的密钥内容"
                required
              />
              <div class="row-actions">
                <button class="btn btn-small btn-primary" type="submit" :disabled="busy[row.id] !== undefined">
                  确认轮换
                </button>
                <button class="btn btn-small" type="button" @click="rotating = null">取消</button>
              </div>
              <RequestError v-if="rotateError !== null" :error="rotateError" />
            </form>
          </td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<style scoped>
.credentials-title {
  margin: 0 0 4px;
  font-size: 20px;
  font-weight: 600;
}

.credentials-hint {
  margin: 0;
  color: #909399;
}

.credentials-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 20px;
}

.credentials-empty {
  padding: 32px;
  color: #909399;
  text-align: center;
  background: #fff;
  border: 1px solid var(--opspilot-border);
  border-radius: 8px;
}

.toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  color: #606266;
  font-size: 13px;
}

.credentials-table {
  width: 100%;
  background: #fff;
  border: 1px solid var(--opspilot-border);
  border-collapse: collapse;
  border-radius: 8px;
}

.credentials-table th,
.credentials-table td {
  padding: 12px 14px;
  border-bottom: 1px solid var(--opspilot-border);
  font-size: 13px;
  text-align: left;
  vertical-align: top;
}

.credentials-table th {
  color: #909399;
  font-weight: 500;
  background: #fafafa;
}

.credentials-table tr:last-child td {
  border-bottom: none;
}

.cred-name {
  font-weight: 600;
}

.cred-sub {
  margin-top: 2px;
  color: #909399;
  font-size: 12px;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  word-break: break-all;
}

.col-actions {
  width: 240px;
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

.rotate-form {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 8px;
}

.pill {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
}

.pill-active {
  color: #67c23a;
  background: #f0f9eb;
}

.pill-retired {
  color: #909399;
  background: #f4f4f5;
}

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

.field-secret {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}

.field-note {
  color: #909399;
  font-size: 12px;
}

.add-form-actions {
  margin-top: 16px;
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
</style>
