/**
 * Task engine endpoints (S2).
 *
 * Creating a task is idempotent: pass `idempotencyKey` and a repeat submit
 * returns the original task (HTTP 200) instead of queueing a second one.
 *
 * Cancel refuses without `confirm: true` — the backend is the control, not a
 * hidden button.
 */
import { api } from '@/api/client'
import type {
  RunResolveBody,
  RunResolveOut,
  TaskCancelBody,
  TaskDetailOut,
  TaskLogPage,
  TaskOut,
  TaskRetryBody,
} from '@/types/api'

export interface CreateTaskBody {
  kind: string
  title?: string
  params?: Record<string, unknown>
  targets: { server_id: number }[]
}

export function listTasks(options?: { limit?: number }): Promise<TaskOut[]> {
  const limit = options?.limit ?? 50
  return api.get<TaskOut[]>(`/api/v1/tasks?limit=${encodeURIComponent(limit)}`)
}

export function getTask(id: number): Promise<TaskDetailOut> {
  return api.get<TaskDetailOut>(`/api/v1/tasks/${id}`)
}

export function createTask(body: CreateTaskBody, idempotencyKey?: string): Promise<TaskOut> {
  // `exactOptionalPropertyTypes` means an optional field must be omitted, not
  // passed as `undefined` — the header is simply left out when there is no key.
  return api.post<TaskOut>(
    '/api/v1/tasks',
    body,
    idempotencyKey === undefined ? {} : { idempotencyKey },
  )
}

export function cancelTask(id: number, body: TaskCancelBody): Promise<TaskOut> {
  return api.post<TaskOut>(`/api/v1/tasks/${id}/cancel`, body)
}

export function retryTask(id: number, body: TaskRetryBody = {}): Promise<TaskOut> {
  return api.post<TaskOut>(`/api/v1/tasks/${id}/retry`, body)
}

/**
 * Paginated log history for one run. `since_seq` is a cursor, not a page
 * number — pass the `next_seq` from the previous page.
 */
export function listRunLogs(runId: number, sinceSeq = 0, limit = 200): Promise<TaskLogPage> {
  const query = `since_seq=${encodeURIComponent(sinceSeq)}&limit=${encodeURIComponent(limit)}`
  return api.get<TaskLogPage>(`/api/v1/task-runs/${runId}/logs?${query}`)
}

/**
 * The only way out of `UNKNOWN`. A human states what actually happened;
 * the platform never guesses.
 */
export function resolveRun(runId: number, body: RunResolveBody): Promise<RunResolveOut> {
  return api.post<RunResolveOut>(`/api/v1/task-runs/${runId}/resolve`, body)
}
