/**
 * Server-Sent Events client for task progress.
 *
 * One `EventSource` per task, opened at `GET /api/v1/tasks/{id}/events`.
 *
 * Authentication is the `?token=` query parameter, not a header: `EventSource`
 * cannot set headers. The token is the same access token used everywhere else,
 * and this endpoint only ever reads. That trade-off is written down in
 * docs/security-model.md rather than left as a surprise.
 *
 * The stream emits named events — `hello`, `log`, `state`, `done` — so each is
 * bound explicitly. A bare `onmessage` would see none of them.
 */
import { getStoredToken } from '@/api/client'

/** One row of `ops_task_logs`, as streamed. */
export interface TaskLogFrame {
  type: 'log'
  task_id: number
  run_id: number
  seq: number
  level: string
  stream: string
  message: string
  ts: string
}

/** Folded statuses whenever any of them changes. */
export interface TaskStateFrame {
  type: 'state'
  task_id: number
  task: string
  runs: Record<string, string>
}

export interface TaskDoneFrame {
  type: 'done'
  task_id: number
  status: string
}

export interface TaskHelloFrame {
  type: 'hello'
  task_id: number
}

export type TaskFrame = TaskLogFrame | TaskStateFrame | TaskDoneFrame | TaskHelloFrame

export interface TaskStreamHandlers {
  onLog?: (frame: TaskLogFrame) => void
  onState?: (frame: TaskStateFrame) => void
  onDone?: (frame: TaskDoneFrame) => void
  onOpen?: () => void
  onError?: (event: Event) => void
}

function parse<T extends TaskFrame>(event: MessageEvent<string>): T | null {
  try {
    return JSON.parse(event.data) as T
  } catch {
    // A malformed frame must not tear down the stream; drop it and carry on.
    return null
  }
}

/** A live task event stream. One instance per task id. */
export class TaskEventSource {
  private source: EventSource | null = null
  private readonly taskId: number
  private readonly handlers: TaskStreamHandlers

  constructor(taskId: number, handlers: TaskStreamHandlers) {
    this.taskId = taskId
    this.handlers = handlers
  }

  /** Open the stream. Safe to call more than once; call `close` to tear down. */
  open(): void {
    if (this.source !== null) {
      return
    }
    const token = getStoredToken()
    const query = token === null ? '' : `?token=${encodeURIComponent(token)}`
    const url = `/api/v1/tasks/${this.taskId}/events${query}`

    const source = new EventSource(url, { withCredentials: true })
    source.onopen = () => this.handlers.onOpen?.()
    source.onerror = (event) => this.handlers.onError?.(event)
    source.addEventListener('log', (event) => {
      const frame = parse<TaskLogFrame>(event as MessageEvent<string>)
      if (frame !== null) this.handlers.onLog?.(frame)
    })
    source.addEventListener('state', (event) => {
      const frame = parse<TaskStateFrame>(event as MessageEvent<string>)
      if (frame !== null) this.handlers.onState?.(frame)
    })
    source.addEventListener('done', (event) => {
      const frame = parse<TaskDoneFrame>(event as MessageEvent<string>)
      if (frame !== null) this.handlers.onDone?.(frame)
    })
    this.source = source
  }

  close(): void {
    this.source?.close()
    this.source = null
  }

  get isOpen(): boolean {
    return this.source !== null && this.source.readyState === EventSource.OPEN
  }
}
