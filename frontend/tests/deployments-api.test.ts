/**
 * Contract tests for the deployment API surface (S4).
 *
 * The two things worth locking are the ones that change what is running on a
 * host: `Idempotency-Key` travels only when the caller supplies one, and a
 * rollback body cannot be sent without `confirm: true`. Both are enforced
 * server-side; these tests make sure the client does not quietly lose them.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  deployRelease,
  getDeployment,
  listApplicationDeployments,
  listDeployments,
  rollbackDeployment,
} from '@/api/deployments'

function jsonResponse(status: number, body: unknown) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function headerOf(init: RequestInit | undefined, name: string): string | undefined {
  const headers = init?.headers
  if (headers === undefined) return undefined
  if (headers instanceof Headers) return headers.get(name) ?? undefined
  if (Array.isArray(headers)) {
    return headers.find(([key]) => key.toLowerCase() === name.toLowerCase())?.[1]
  }
  return headers[name] ?? headers[name.toLowerCase()]
}

const CHECKSUM = 'a'.repeat(64)

const DEPLOYMENT = {
  id: 12,
  application_id: 7,
  environment_id: 3,
  kind: 'DEPLOY',
  status: 'PENDING',
  release_version: '1.4.2',
  release_id: '1.4.2',
  previous_release_id: '1.4.1',
  artifact_url: 'https://artifacts.example.com/billing-1.4.2.tar.gz',
  artifact_checksum: CHECKSUM,
  task_id: 44,
  rollback_of: null,
  requested_by: 'admin',
  error_detail: '',
  started_at: null,
  finished_at: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const BODY = {
  environment: 'production' as const,
  release_version: '1.4.2',
  artifact_url: 'https://artifacts.example.com/billing-1.4.2.tar.gz',
  artifact_checksum: CHECKSUM,
  targets: [{ server_id: 1 }],
  module_name: 'billing.main',
  config: { LOG_LEVEL: 'info' },
}

describe('deployments api', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('lists the whole history, or one application', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(200, [DEPLOYMENT]),
    )
    vi.stubGlobal('fetch', fetchMock)

    await listDeployments({ limit: 20 })
    await listDeployments({ applicationId: 7, limit: 20, offset: 40 })
    await listApplicationDeployments(7, 20)

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/deployments?limit=20')
    expect(fetchMock.mock.calls[1]?.[0]).toBe(
      '/api/v1/deployments?application_id=7&limit=20&offset=40',
    )
    expect(fetchMock.mock.calls[2]?.[0]).toBe('/api/v1/applications/7/deployments?limit=20')
  })

  it('reads one deployment record', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(200, DEPLOYMENT),
    )
    vi.stubGlobal('fetch', fetchMock)

    await getDeployment(12)

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/deployments/12')
  })

  it('submits a pinned release against the application deploy route', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(202, DEPLOYMENT),
    )
    vi.stubGlobal('fetch', fetchMock)

    await deployRelease(7, BODY)

    const init = fetchMock.mock.calls[0]?.[1]
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/applications/7/deploy')
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toEqual(BODY)
  })

  it('sends Idempotency-Key only when one is supplied', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(202, DEPLOYMENT),
    )
    vi.stubGlobal('fetch', fetchMock)

    await deployRelease(7, BODY)
    expect(headerOf(fetchMock.mock.calls[0]?.[1], 'Idempotency-Key')).toBeUndefined()

    await deployRelease(7, BODY, 'deploy-1')
    expect(headerOf(fetchMock.mock.calls[1]?.[1], 'Idempotency-Key')).toBe('deploy-1')
  })

  it('always confirms a rollback, and omits a blank reason', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(202, { ...DEPLOYMENT, kind: 'ROLLBACK', rollback_of: 12 }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await rollbackDeployment(12, { targets: [{ server_id: 1 }], confirm: true })

    const init = fetchMock.mock.calls[0]?.[1]
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/deployments/12/rollback')
    expect(JSON.parse(String(init?.body))).toEqual({
      targets: [{ server_id: 1 }],
      confirm: true,
    })
  })

  it('carries the reason through when one is given', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(202, DEPLOYMENT),
    )
    vi.stubGlobal('fetch', fetchMock)

    await rollbackDeployment(12, {
      targets: [{ server_id: 1 }],
      reason: '健康检查不过',
      confirm: true,
    })

    const init = fetchMock.mock.calls[0]?.[1]
    expect(JSON.parse(String(init?.body))).toEqual({
      targets: [{ server_id: 1 }],
      reason: '健康检查不过',
      confirm: true,
    })
  })

  it('surfaces a 409 when the environment is already busy', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(409, {
          code: 'conflict',
          message: 'environment 3 already has deployment 11 in state RUNNING',
          request_id: 'req-9',
        }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(deployRelease(7, BODY)).rejects.toMatchObject({
      status: 409,
      code: 'conflict',
    })
  })
})
