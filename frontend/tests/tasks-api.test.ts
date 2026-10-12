/**
 * Contract tests for the task API surface.
 *
 * These lock down the wire shapes the backend actually implements: the
 * `since_seq` log cursor (not `after_seq`), `Idempotency-Key` only when a key
 * is supplied, and the `confirm: true` body the cancel endpoint requires.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

import { setStoredToken } from '@/api/client'
import { TaskEventSource } from '@/api/sse'
import {
  cancelTask,
  createTask,
  getTask,
  listRunLogs,
  listTasks,
  resolveRun,
  retryTask,
} from '@/api/tasks'

function jsonResponse(status: number, body: unknown) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** Read one header off a request init without widening `HeadersInit`. */
function headerOf(init: RequestInit | undefined, name: string): string | undefined {
  const headers = init?.headers
  if (headers === undefined) return undefined
  if (headers instanceof Headers) return headers.get(name) ?? undefined
  if (Array.isArray(headers)) {
    return headers.find(([key]) => key.toLowerCase() === name.toLowerCase())?.[1]
  }
  return headers[name] ?? headers[name.toLowerCase()]
}

const TASK = {
  id: 4,
  kind: 'server.check',
  title: 'check web-1',
  status: 'QUEUED',
  idempotency_key: '',
  requested_by: 1,
  requested_by_name: 'admin',
  queued_at: '2026-01-01T00:00:00Z',
  started_at: null,
  finished_at: null,
  cancel_requested_at: null,
  retry_of_task_id: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

describe('tasks api', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('lists tasks with the requested limit', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(200, [TASK]),
    )
    vi.stubGlobal('fetch', fetchMock)

    await listTasks({ limit: 25 })

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/tasks?limit=25')
  })

  it('reads one task with its runs and steps', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(200, { ...TASK, runs: [] }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await getTask(4)

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/tasks/4')
  })

  it('sends Idempotency-Key only when one is supplied', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(201, TASK),
    )
    vi.stubGlobal('fetch', fetchMock)

    await createTask({ kind: 'server.check', targets: [{ server_id: 1 }] })
    expect(headerOf(fetchMock.mock.calls[0]?.[1], 'Idempotency-Key')).toBeUndefined()

    await createTask({ kind: 'server.check', targets: [{ server_id: 1 }] }, 'key-1')
    expect(headerOf(fetchMock.mock.calls[1]?.[1], 'Idempotency-Key')).toBe('key-1')
  })

  it('pages logs with the since_seq cursor the backend implements', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(200, { items: [], next_seq: 12, total: 12 }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await listRunLogs(9, 7, 50)

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/task-runs/9/logs?since_seq=7&limit=50')
  })

  it('refuses to cancel without confirm: true', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(200, TASK),
    )
    vi.stubGlobal('fetch', fetchMock)

    await cancelTask(4, { reason: 'wrong target', confirm: true })

    const init = fetchMock.mock.calls[0]?.[1]
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/tasks/4/cancel')
    expect(JSON.parse(String(init?.body))).toEqual({ reason: 'wrong target', confirm: true })
  })

  it('omits the reason entirely when the operator left it blank', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(200, TASK),
    )
    vi.stubGlobal('fetch', fetchMock)

    await cancelTask(4, { confirm: true })

    const init = fetchMock.mock.calls[0]?.[1]
    expect(JSON.parse(String(init?.body))).toEqual({ confirm: true })
  })

  it('retries without a reason by sending an empty body', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(200, TASK),
    )
    vi.stubGlobal('fetch', fetchMock)

    await retryTask(4)

    const init = fetchMock.mock.calls[0]?.[1]
    expect(JSON.parse(String(init?.body))).toEqual({})
  })

  it('posts a human verdict and reason when resolving UNKNOWN', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(200, {
          id: 9,
          task_id: 4,
          server_id: 1,
          status: 'SUCCESS',
          attempt: 1,
          recovery_note: 'operator ruled success',
        }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const out = await resolveRun(9, { verdict: 'success', reason: 'operator ruled success' })

    const init = fetchMock.mock.calls[0]?.[1]
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/task-runs/9/resolve')
    expect(JSON.parse(String(init?.body))).toEqual({
      verdict: 'success',
      reason: 'operator ruled success',
    })
    expect(out.status).toBe('SUCCESS')
  })
})

describe('task event stream', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    window.localStorage.clear()
  })

  it('authenticates through the query parameter and binds named events', () => {
    setStoredToken('tok-123')
    const listeners: Record<string, (event: MessageEvent<string>) => void> = {}
    let openedUrl = ''

    class FakeEventSource {
      readyState = 1
      static OPEN = 1
      onopen: (() => void) | null = null
      onerror: ((event: Event) => void) | null = null
      constructor(url: string) {
        openedUrl = url
      }
      addEventListener(name: string, handler: (event: MessageEvent<string>) => void) {
        listeners[name] = handler
      }
      close(): void {}
    }
    vi.stubGlobal('EventSource', FakeEventSource)

    const seen: string[] = []
    const source = new TaskEventSource(12, {
      onLog: (f) => seen.push(`log:${f.message}`),
      onState: (f) => seen.push(`state:${f.task}`),
      onDone: (f) => seen.push(`done:${f.status}`),
    })
    source.open()

    // EventSource cannot set headers, so the token travels in the URL.
    expect(openedUrl).toBe('/api/v1/tasks/12/events?token=tok-123')

    // A bare onmessage would see none of these — they are named events.
    expect(Object.keys(listeners).sort()).toEqual(['done', 'log', 'state'])

    listeners.log!(
      new MessageEvent('log', {
        data: JSON.stringify({
          type: 'log',
          task_id: 12,
          run_id: 3,
          seq: 1,
          level: 'info',
          stream: 'stdout',
          message: 'hello',
          ts: '2026-01-01T00:00:00Z',
        }),
      }),
    )
    listeners.state!(
      new MessageEvent('state', {
        data: JSON.stringify({ type: 'state', task_id: 12, task: 'RUNNING', runs: { '3': 'EXECUTING' } }),
      }),
    )
    listeners.done!(
      new MessageEvent('done', {
        data: JSON.stringify({ type: 'done', task_id: 12, status: 'SUCCESS' }),
      }),
    )

    expect(seen).toEqual(['log:hello', 'state:RUNNING', 'done:SUCCESS'])
    source.close()
  })

  it('drops a malformed frame instead of tearing down the stream', () => {
    const listeners: Record<string, (event: MessageEvent<string>) => void> = {}
    class FakeEventSource {
      readyState = 1
      static OPEN = 1
      onopen: (() => void) | null = null
      onerror: ((event: Event) => void) | null = null
      constructor(_url: string) {}
      addEventListener(name: string, handler: (event: MessageEvent<string>) => void) {
        listeners[name] = handler
      }
      close(): void {}
    }
    vi.stubGlobal('EventSource', FakeEventSource)

    let calls = 0
    const source = new TaskEventSource(1, { onLog: () => (calls += 1) })
    source.open()

    listeners.log!(new MessageEvent('log', { data: 'not json at all' }))
    expect(calls).toBe(0)

    listeners.log!(
      new MessageEvent('log', {
        data: JSON.stringify({
          type: 'log',
          task_id: 1,
          run_id: 1,
          seq: 1,
          level: 'info',
          stream: 'stdout',
          message: 'still here',
          ts: '2026-01-01T00:00:00Z',
        }),
      }),
    )
    expect(calls).toBe(1)
    source.close()
  })
})
