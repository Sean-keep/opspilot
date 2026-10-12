/**
 * Server-Sent Events client for task progress.
 *
 * S0 ships the interface only: the task engine and its event stream arrive in
 * S2. The shape is fixed now so views can be written against a stable API and
 * swapped onto the real endpoint without touching call sites.
 *
 * The stream is authenticated the same way as REST — a bearer token is not
 * something EventSource can send, so the token travels as a query parameter
 * and the backend accepts it for this one endpoint. That is documented in
 * docs/security-model.md.
 */
import { getStoredToken } from '@/api/client'

export interface TaskEvent {
  /** Event name: 'step', 'log', 'status', 'heartbeat', 'end'. */
  type: string
  task_id: number
  run_id?: number
  step_key?: string
  attempt?: number
  status?: string
  message?: string
  at: string
  data?: Record<string, unknown>
}

export type TaskEventHandler = (event: TaskEvent) => void

export interface TaskStreamOptions {
  onEvent: TaskEventHandler
  onError?: (error: Event) => void
  onOpen?: () => void
}

/** A live task event stream. One instance per task id. */
export class TaskEventSource {
  private source: EventSource | null = null
  private readonly taskId: number
  private readonly options: TaskStreamOptions

  constructor(taskId: number, options: TaskStreamOptions) {
    this.taskId = taskId
    this.options = options
  }

  /** Open the stream. Safe to call once; call ``close`` to tear it down. */
  open(): void {
    if (this.source !== null) {
      return
    }
    const token = getStoredToken()
    const query = token ? `?access_token=${encodeURIComponent(token)}` : ''
    const url = `/api/v1/tasks/${this.taskId}/events${query}`

    this.source = new EventSource(url, { withCredentials: true })
    this.source.onopen = () => this.options.onOpen?.()
    this.source.onerror = (event) => this.options.onError?.(event)
    this.source.onmessage = (message) => {
      try {
        const parsed = JSON.parse(message.data) as TaskEvent
        this.options.onEvent(parsed)
      } catch {
        // A malformed frame must not tear down the stream; drop it and go on.
      }
    }
  }

  close(): void {
    this.source?.close()
    this.source = null
  }

  get isOpen(): boolean {
    return this.source !== null && this.source.readyState === EventSource.OPEN
  }
}
