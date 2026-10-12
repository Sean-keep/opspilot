<script setup lang="ts">
/**
 * S2 task detail: one task, its per-server runs, the steps inside each run,
 * and the log history that proves what actually happened.
 *
 * Live progress arrives over SSE. The history below it is loaded over REST
 * with a `since_seq` cursor — that is the durable record, the stream is just
 * the fast path while the run is still writing.
 *
 * The three post-mortem actions are deliberately different:
 *  - cancel stops further work and does **not** undo completed steps;
 *  - retry re-opens finished runs as a new attempt, but never an `UNKNOWN`;
 *  - resolve is the only way out of `UNKNOWN`, and it always takes a human
 *    verdict plus a reason. The platform never guesses.
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { TaskEventSource, type TaskLogFrame, type TaskStateFrame } from '@/api/sse'
import { cancelTask, getTask, listRunLogs, resolveRun, retryTask } from '@/api/tasks'
import PermissionGate from '@/components/PermissionGate.vue'
import RequestError from '@/components/RequestError.vue'
import type {
  RunVerdict,
  TaskDetailOut,
  TaskLogOut,
  TaskRunOut,
  TaskStepOut,
} from '@/types/api'

const route = useRoute()
const router = useRouter()

const task = ref<TaskDetailOut | null>(null)
const loading = ref(false)
const loadError = ref<unknown>(null)
const actionError = ref<unknown>(null)
const actionBusy = ref('')

// Live log rows accumulated from the stream, keyed by run id. History is
// fetched separately so a refresh still shows everything.
const liveLogs = ref<Record<number, TaskLogOut[]>>({})
const historyLogs = ref<Record<number, TaskLogOut[]>>({})
const historyCursor = ref<Record<number, number>>({})
const historyTotal = ref<Record<number, number>>({})
const historyLoading = ref<Record<number, boolean>>({})

// Which run's log panel is expanded. Only one at a time keeps the page tall
// enough to read.
const openRunId = ref<number | null>(null)

// -- cancel / retry / resolve dialogs ---------------------------------------
const cancelOpen = ref(false)
const cancelReason = ref('')
const retryOpen = ref(false)
const retryReason = ref('')
const resolveTarget = ref<TaskRunOut | null>(null)
const resolveVerdict = ref<RunVerdict>('success')
const resolveReason = ref('')

const taskId = computed(() => Number(route.params.id))

const stream = ref<TaskEventSource | null>(null)

const unknownRuns = computed(() => (task.value?.runs ?? []).filter((r) => r.status === 'UNKNOWN'))

function runName(run: TaskRunOut): string {
  return `服务器 #${run.server_id}`
}

function statusLabel(status: string): string {
  const map: Record<string, string> = {
    PENDING: '待处理',
    QUEUED: '已排队',
    RUNNING: '执行中',
    SUCCESS: '成功',
    PARTIAL_SUCCESS: '部分成功',
    FAILED: '失败',
    CANCELLING: '取消中',
    CANCELLED: '已取消',
    WAITING: '等待',
    PRECHECK: '预检',
    EXECUTING: '执行',
    VERIFYING: '验证',
    UNKNOWN: '结果未知',
  }
  return map[status] ?? status
}

function stepLabel(status: string): string {
  const map: Record<string, string> = {
    PENDING: '待执行',
    RUNNING: '执行中',
    SUCCESS: '成功',
    FAILED: '失败',
    SKIPPED: '跳过',
    CANCELLED: '已取消',
    UNKNOWN: '结果未知',
  }
  return map[status] ?? status
}

function statusClass(status: string): string {
  return `pill-${status.toLowerCase().replace(/_/g, '-')}`
}

function idempotencyLabel(value: string): string {
  const map: Record<string, string> = {
    SAFE: '幂等',
    CONDITIONAL: '条件幂等',
    DESTRUCTIVE: '非幂等',
  }
  return map[value] ?? value
}

function formatTime(value: string | null): string {
  if (value === null || value === '') return '—'
  return new Date(value).toLocaleString()
}

function formatLogTime(value: string): string {
  return new Date(value).toLocaleTimeString()
}

function formatResult(step: TaskStepOut): string {
  if (step.result === null) return ''
  try {
    return JSON.stringify(step.result)
  } catch {
    return ''
  }
}

function mergedLogs(runId: number): TaskLogOut[] {
  const seen = new Map<number, TaskLogOut>()
  for (const row of historyLogs.value[runId] ?? []) seen.set(row.seq, row)
  for (const row of liveLogs.value[runId] ?? []) seen.set(row.seq, row)
  return [...seen.values()].sort((a, b) => a.seq - b.seq)
}

async function load(): Promise<void> {
  loading.value = true
  try {
    task.value = await getTask(taskId.value)
    loadError.value = null
  } catch (cause) {
    loadError.value = cause
  } finally {
    loading.value = false
  }
}

async function loadHistory(runId: number, reset = false): Promise<void> {
  if (historyLoading.value[runId] === true) return
  historyLoading.value = { ...historyLoading.value, [runId]: true }
  try {
    const since = reset ? 0 : (historyCursor.value[runId] ?? 0)
    const page = await listRunLogs(runId, since, 200)
    const existing = reset ? [] : (historyLogs.value[runId] ?? [])
    const seen = new Set(existing.map((r) => r.seq))
    const fresh = page.items.filter((r) => !seen.has(r.seq))
    historyLogs.value = { ...historyLogs.value, [runId]: [...existing, ...fresh] }
    historyCursor.value = { ...historyCursor.value, [runId]: page.next_seq }
    historyTotal.value = { ...historyTotal.value, [runId]: page.total }
  } catch (cause) {
    actionError.value = cause
  } finally {
    const next = { ...historyLoading.value }
    delete next[runId]
    historyLoading.value = next
  }
}

function toggleRun(runId: number): void {
  if (openRunId.value === runId) {
    openRunId.value = null
    return
  }
  openRunId.value = runId
  void loadHistory(runId, true)
}

/** Open the verdict dialog for one `UNKNOWN` run with a clean form. */
function startResolve(run: TaskRunOut): void {
  resolveTarget.value = run
  resolveVerdict.value = 'success'
  resolveReason.value = ''
}

function openStream(): void {
  stream.value?.close()
  liveLogs.value = {}
  const source = new TaskEventSource(taskId.value, {
    onLog: (frame: TaskLogFrame) => {
      const rows = liveLogs.value[frame.run_id] ?? []
      if (rows.some((r) => r.seq === frame.seq)) return
      liveLogs.value = {
        ...liveLogs.value,
        [frame.run_id]: [
          ...rows,
          {
            id: frame.seq,
            run_id: frame.run_id,
            step_id: null,
            seq: frame.seq,
            ts: frame.ts,
            level: frame.level,
            stream: frame.stream,
            message: frame.message,
          },
        ],
      }
    },
    onState: (frame: TaskStateFrame) => {
      const current = task.value
      if (current === null) return
      task.value = {
        ...current,
        status: frame.task as TaskDetailOut['status'],
        runs: current.runs.map((r) => {
          const next = frame.runs[String(r.id)]
          return next === undefined ? r : { ...r, status: next as TaskRunOut['status'] }
        }),
      }
    },
    onDone: () => {
      // The stream says the story is over; reload so the final rows — including
      // any the stream dropped — come from the durable store.
      stream.value?.close()
      stream.value = null
      void load().then(() => {
        if (openRunId.value !== null) void loadHistory(openRunId.value, true)
      })
    },
    onError: () => {
      // EventSource retries on its own. If the task is already terminal there
      // is nothing left to watch, so stop rather than spin.
      if (task.value !== null && isTerminal(task.value.status)) {
        stream.value?.close()
        stream.value = null
      }
    },
  })
  source.open()
  stream.value = source
}

function isTerminal(status: string): boolean {
  return ['SUCCESS', 'PARTIAL_SUCCESS', 'FAILED', 'CANCELLED'].includes(status)
}

// -- actions ----------------------------------------------------------------

async function doCancel(): Promise<void> {
  const current = task.value
  if (current === null) return
  actionBusy.value = 'cancel'
  actionError.value = null
  try {
    const reason = cancelReason.value.trim()
    await cancelTask(current.id, reason === '' ? { confirm: true } : { reason, confirm: true })
    cancelOpen.value = false
    cancelReason.value = ''
    await load()
  } catch (cause) {
    actionError.value = cause
  } finally {
    actionBusy.value = ''
  }
}

async function doRetry(): Promise<void> {
  const current = task.value
  if (current === null) return
  actionBusy.value = 'retry'
  actionError.value = null
  try {
    const reason = retryReason.value.trim()
    await retryTask(current.id, reason === '' ? {} : { reason })
    retryOpen.value = false
    retryReason.value = ''
    await load()
    openStream()
  } catch (cause) {
    actionError.value = cause
  } finally {
    actionBusy.value = ''
  }
}

async function doResolve(): Promise<void> {
  const run = resolveTarget.value
  if (run === null) return
  actionBusy.value = 'resolve'
  actionError.value = null
  try {
    await resolveRun(run.id, {
      verdict: resolveVerdict.value,
      reason: resolveReason.value.trim(),
    })
    resolveTarget.value = null
    resolveReason.value = ''
    await load()
  } catch (cause) {
    actionError.value = cause
  } finally {
    actionBusy.value = ''
  }
}

function confirmCancel(): void {
  if (!window.confirm('确定请求取消？已完成的步骤不会被撤销，回滚需要另外发起任务。')) return
  cancelOpen.value = true
}

watch(
  taskId,
  () => {
    void load().then(openStream)
  },
  { immediate: false },
)

onMounted(() => {
  void load().then(openStream)
})

onUnmounted(() => {
  stream.value?.close()
  stream.value = null
})
</script>

<template>
  <section class="task-detail">
    <header class="detail-header">
      <div>
        <button class="btn btn-small" type="button" @click="router.push({ name: 'tasks' })">
          ← 返回列表
        </button>
        <h1 class="detail-title">
          {{ task?.title || `任务 #${taskId}` }}
          <span v-if="task" class="pill" :class="statusClass(task.status)">
            {{ statusLabel(task.status) }}
          </span>
        </h1>
        <p v-if="task" class="detail-meta">
          <span class="mono">{{ task.kind }}</span>
          · 发起人 {{ task.requested_by_name || '—' }}
          · 开始 {{ formatTime(task.started_at) }}
          · 结束 {{ formatTime(task.finished_at) }}
          <template v-if="task.retry_of_task_id !== null"> · 重试自 #{{ task.retry_of_task_id }}</template>
        </p>
      </div>
      <div v-if="task" class="detail-actions">
        <PermissionGate permission="task.cancel">
          <button
            v-if="!isTerminal(task.status)"
            class="btn btn-danger"
            type="button"
            :disabled="actionBusy !== ''"
            @click="confirmCancel"
          >
            请求取消
          </button>
        </PermissionGate>
        <PermissionGate permission="task.retry">
          <button
            v-if="isTerminal(task.status) && task.status !== 'SUCCESS'"
            class="btn"
            type="button"
            :disabled="actionBusy !== ''"
            @click="retryOpen = true"
          >
            重试
          </button>
        </PermissionGate>
      </div>
    </header>

    <RequestError v-if="loadError !== null" :error="loadError" />
    <RequestError v-if="actionError !== null" :error="actionError" />

    <div v-if="task && task.cancel_requested_at !== null && !isTerminal(task.status)" class="notice">
      已请求取消（{{ formatTime(task.cancel_requested_at) }}）。Worker 会在步骤边界停止，并把远端证据写进步骤结果。
    </div>

    <div v-if="loading && task === null" class="detail-empty">加载中…</div>

    <div v-if="task" class="runs">
      <article v-for="run in task.runs" :key="run.id" class="run">
        <header class="run-header">
          <div>
            <h2 class="run-title">
              {{ runName(run) }}
              <span class="pill" :class="statusClass(run.status)">{{ statusLabel(run.status) }}</span>
            </h2>
            <p class="run-meta">
              尝试 #{{ run.attempt }}
              · 开始 {{ formatTime(run.started_at) }}
              · 结束 {{ formatTime(run.finished_at) }}
              <template v-if="run.lease_owner"> · 执行者 {{ run.lease_owner }}</template>
            </p>
            <p v-if="run.error_detail" class="run-error">{{ run.error_detail }}</p>
            <p v-if="run.recovery_note" class="run-note">{{ run.recovery_note }}</p>
          </div>
          <div class="run-actions">
            <PermissionGate permission="task.resolve">
              <button
                v-if="run.status === 'UNKNOWN'"
                class="btn btn-small"
                type="button"
                @click="startResolve(run)"
              >
                人工裁定
              </button>
            </PermissionGate>
            <button class="btn btn-small" type="button" @click="toggleRun(run.id)">
              {{ openRunId === run.id ? '收起日志' : '查看日志' }}
            </button>
          </div>
        </header>

        <table v-if="run.steps.length > 0" class="steps-table">
          <thead>
            <tr>
              <th>步骤</th>
              <th>动作</th>
              <th>状态</th>
              <th>幂等</th>
              <th>退出码</th>
              <th>耗时</th>
              <th>结果</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="step in run.steps" :key="`${step.step_key}-${step.attempt}`">
              <td>
                <span class="mono">{{ step.step_key }}</span>
                <div class="step-sub">第 {{ step.attempt }} 次</div>
              </td>
              <td class="mono">{{ step.action }}</td>
              <td>
                <span class="pill" :class="statusClass(step.status)">{{ stepLabel(step.status) }}</span>
              </td>
              <td>{{ idempotencyLabel(step.idempotency_class) }}</td>
              <td class="mono">{{ step.exit_code ?? '—' }}</td>
              <td>{{ step.duration_ms === null ? '—' : `${step.duration_ms} ms` }}</td>
              <td class="result-cell mono">{{ formatResult(step) }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="run-empty">尚未产生步骤记录。</p>

        <div v-if="openRunId === run.id" class="logs">
          <header class="logs-header">
            <h3 class="logs-title">
              执行日志
              <span class="logs-count">
                {{ mergedLogs(run.id).length }} / {{ historyTotal[run.id] ?? 0 }} 行
              </span>
            </h3>
            <button
              class="btn btn-small"
              type="button"
              :disabled="historyLoading[run.id] === true"
              @click="void loadHistory(run.id, true)"
            >
              刷新
            </button>
          </header>
          <div v-if="mergedLogs(run.id).length === 0" class="logs-empty">还没有日志。</div>
          <pre v-else class="logs-body"><code><span
            v-for="row in mergedLogs(run.id)"
            :key="row.seq"
            class="log-line"
            :class="`log-${row.level}`"
          >[{{ formatLogTime(row.ts) }}] [{{ row.level }}] [{{ row.stream }}] {{ row.message }}
</span></code></pre>
          <button
            v-if="mergedLogs(run.id).length < (historyTotal[run.id] ?? 0)"
            class="btn btn-small logs-more"
            type="button"
            :disabled="historyLoading[run.id] === true"
            @click="void loadHistory(run.id, false)"
          >
            加载更多日志
          </button>
        </div>
      </article>
    </div>

    <!-- -- cancel dialog ------------------------------------------------- -->
    <div v-if="cancelOpen" class="modal-backdrop" @click.self="cancelOpen = false">
      <form class="modal" @submit.prevent="doCancel">
        <h2 class="modal-title">请求取消任务</h2>
        <p class="modal-note">
          取消只会停止后续步骤，<strong>不会回滚</strong>已经完成的动作。回滚需要另外发起任务。
        </p>
        <label class="field">
          <span class="field-label">原因（可选）</span>
          <input v-model="cancelReason" class="field-input" maxlength="500" />
        </label>
        <div class="modal-actions">
          <button class="btn" type="button" @click="cancelOpen = false">返回</button>
          <button class="btn btn-danger" type="submit" :disabled="actionBusy !== ''">
            {{ actionBusy === 'cancel' ? '提交中…' : '确认取消' }}
          </button>
        </div>
      </form>
    </div>

    <!-- -- retry dialog -------------------------------------------------- -->
    <div v-if="retryOpen" class="modal-backdrop" @click.self="retryOpen = false">
      <form class="modal" @submit.prevent="doRetry">
        <h2 class="modal-title">重试任务</h2>
        <p class="modal-note">
          会把未成功的服务器重新排队。状态为「结果未知」的执行必须先人工裁定，不会被自动重试。
        </p>
        <p v-if="unknownRuns.length > 0" class="modal-warn">
          当前有 {{ unknownRuns.length }} 个执行结果未知，重试会跳过它们。
        </p>
        <label class="field">
          <span class="field-label">原因（可选）</span>
          <input v-model="retryReason" class="field-input" maxlength="500" />
        </label>
        <div class="modal-actions">
          <button class="btn" type="button" @click="retryOpen = false">返回</button>
          <button class="btn btn-primary" type="submit" :disabled="actionBusy !== ''">
            {{ actionBusy === 'retry' ? '提交中…' : '确认重试' }}
          </button>
        </div>
      </form>
    </div>

    <!-- -- resolve dialog ------------------------------------------------ -->
    <div v-if="resolveTarget !== null" class="modal-backdrop" @click.self="resolveTarget = null">
      <form class="modal" @submit.prevent="doResolve">
        <h2 class="modal-title">裁定「结果未知」的执行</h2>
        <p class="modal-note">
          平台无法确认这次远程执行的实际结果，因此不会自动重试。请说明真实情况：
        </p>
        <label class="field">
          <span class="field-label">裁定</span>
          <select v-model="resolveVerdict" class="field-input">
            <option value="success">实际成功了</option>
            <option value="retry">可以安全重试</option>
            <option value="failure">按失败处理</option>
          </select>
        </label>
        <label class="field">
          <span class="field-label">依据（必填）</span>
          <input
            v-model="resolveReason"
            class="field-input"
            required
            maxlength="500"
            placeholder="例如：已在目标机上确认服务已启动"
          />
        </label>
        <div class="modal-actions">
          <button class="btn" type="button" @click="resolveTarget = null">返回</button>
          <button
            class="btn btn-primary"
            type="submit"
            :disabled="actionBusy !== '' || resolveReason.trim() === ''"
          >
            {{ actionBusy === 'resolve' ? '提交中…' : '提交裁定' }}
          </button>
        </div>
      </form>
    </div>
  </section>
</template>

<style scoped>
.detail-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}

.detail-title {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 10px 0 4px;
  font-size: 20px;
  font-weight: 600;
}

.detail-meta {
  margin: 0;
  color: #909399;
  font-size: 13px;
}

.detail-actions {
  display: flex;
  gap: 8px;
}

.detail-empty {
  padding: 32px;
  color: #909399;
  text-align: center;
  background: #fff;
  border: 1px solid var(--opspilot-border);
  border-radius: 8px;
}

.notice {
  margin-bottom: 16px;
  padding: 12px 14px;
  color: #e6a23c;
  background: #fdf6ec;
  border: 1px solid #f5dab1;
  border-radius: 6px;
  font-size: 13px;
}

.runs {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.run {
  padding: 16px;
  background: #fff;
  border: 1px solid var(--opspilot-border);
  border-radius: 8px;
}

.run-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.run-title {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}

.run-meta {
  margin: 4px 0 0;
  color: #909399;
  font-size: 12px;
}

.run-error {
  margin: 6px 0 0;
  color: #f56c6c;
  font-size: 12px;
  word-break: break-all;
}

.run-note {
  margin: 6px 0 0;
  color: #e6a23c;
  font-size: 12px;
}

.run-actions {
  display: flex;
  gap: 6px;
}

.run-empty {
  margin: 0;
  color: #909399;
  font-size: 13px;
}

.steps-table {
  width: 100%;
  border-collapse: collapse;
}

.steps-table th,
.steps-table td {
  padding: 8px 10px;
  border-bottom: 1px solid var(--opspilot-border);
  font-size: 12px;
  text-align: left;
  vertical-align: top;
}

.steps-table th {
  color: #909399;
  font-weight: 500;
  background: #fafafa;
}

.steps-table tr:last-child td {
  border-bottom: none;
}

.step-sub {
  margin-top: 2px;
  color: #909399;
  font-size: 11px;
}

.result-cell {
  max-width: 260px;
  word-break: break-all;
  color: #909399;
}

.logs {
  margin-top: 12px;
  border: 1px solid var(--opspilot-border);
  border-radius: 6px;
}

.logs-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  background: #fafafa;
  border-bottom: 1px solid var(--opspilot-border);
}

.logs-title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0;
  font-size: 13px;
  font-weight: 600;
}

.logs-count {
  color: #909399;
  font-size: 12px;
  font-weight: 400;
}

.logs-empty {
  padding: 16px;
  color: #909399;
  font-size: 12px;
  text-align: center;
}

.logs-body {
  max-height: 360px;
  margin: 0;
  padding: 12px;
  overflow: auto;
  background: #1f2d3d;
  border-radius: 0 0 6px 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.6;
}

.log-line {
  display: block;
  color: #c0c8d2;
  white-space: pre-wrap;
  word-break: break-all;
}

.log-error {
  color: #f56c6c;
}

.log-warning {
  color: #e6a23c;
}

.logs-more {
  margin: 8px 12px 12px;
}

/* -- shared bits kept in the house style of the other views --------------- */
.pill {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
  font-weight: 400;
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

.pill-running,
.pill-executing,
.pill-precheck,
.pill-verifying {
  color: #409eff;
  background: #ecf5ff;
}

.pill-unknown {
  color: #e6a23c;
  background: #fdf6ec;
}

.pill-cancelled,
.pill-cancelling,
.pill-skipped,
.pill-pending,
.pill-queued,
.pill-waiting {
  color: #909399;
  background: #f4f4f5;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  word-break: break-all;
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

.modal-backdrop {
  position: fixed;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.35);
  z-index: 50;
}

.modal {
  width: min(440px, calc(100vw - 32px));
  padding: 20px;
  background: #fff;
  border-radius: 8px;
}

.modal-title {
  margin: 0 0 8px;
  font-size: 16px;
  font-weight: 600;
}

.modal-note {
  margin: 0 0 12px;
  color: #606266;
  font-size: 13px;
  line-height: 1.6;
}

.modal-warn {
  margin: 0 0 12px;
  padding: 8px 10px;
  color: #e6a23c;
  background: #fdf6ec;
  border-radius: 4px;
  font-size: 12px;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
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
</style>
