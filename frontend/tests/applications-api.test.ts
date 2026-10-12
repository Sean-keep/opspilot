/**
 * Contract tests for the application / environment API surface (S4).
 *
 * These lock the nesting the backend actually implements — environments live
 * under their application, and `name` is never sent on an update because
 * renaming would rewrite every deployment record that refers to it.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  createApplication,
  createEnvironment,
  getApplication,
  listApplications,
  updateApplication,
  updateEnvironment,
} from '@/api/applications'

function jsonResponse(status: number, body: unknown) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const ENV = {
  id: 3,
  application_id: 7,
  name: 'production',
  config_ref: 'cred:77',
  service_name: 'opspilot-billing',
  unit_path: '/etc/systemd/system/opspilot-billing.service',
  health_url: 'http://127.0.0.1:8000/healthz',
  health_timeout_s: 5,
  keep_releases: 5,
}

const APP = {
  id: 7,
  name: 'billing-api',
  runtime_type: 'python',
  repo_url: '',
  default_branch: '',
  run_user: 'billing',
  description: '',
  environments: [ENV],
}

describe('applications api', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('lists and reads applications', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(200, [APP]),
    )
    vi.stubGlobal('fetch', fetchMock)

    await listApplications()
    await getApplication(7)

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/applications')
    expect(fetchMock.mock.calls[1]?.[0]).toBe('/api/v1/applications/7')
  })

  it('creates an application with only the fields the operator set', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(201, APP),
    )
    vi.stubGlobal('fetch', fetchMock)

    await createApplication({ name: 'Billing-API', run_user: 'billing' })

    const init = fetchMock.mock.calls[0]?.[1]
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/applications')
    expect(JSON.parse(String(init?.body))).toEqual({ name: 'Billing-API', run_user: 'billing' })
  })

  it('never sends a name on update', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(200, APP),
    )
    vi.stubGlobal('fetch', fetchMock)

    await updateApplication(7, { description: '计费对外 API', run_user: 'svc' })

    const init = fetchMock.mock.calls[0]?.[1]
    expect(init?.method).toBe('PATCH')
    expect(JSON.parse(String(init?.body))).toEqual({
      description: '计费对外 API',
      run_user: 'svc',
    })
  })

  it('nests environments under their application', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(201, ENV),
    )
    vi.stubGlobal('fetch', fetchMock)

    await createEnvironment(7, { name: 'production', config_ref: 'cred:77' })

    const init = fetchMock.mock.calls[0]?.[1]
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/applications/7/environments')
    expect(JSON.parse(String(init?.body))).toEqual({ name: 'production', config_ref: 'cred:77' })
  })

  it('updates an environment in place and never renames it', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(200, ENV),
    )
    vi.stubGlobal('fetch', fetchMock)

    await updateEnvironment(7, 3, { health_timeout_s: 10, service_name: 'opspilot-billing' })

    const init = fetchMock.mock.calls[0]?.[1]
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/applications/7/environments/3')
    expect(init?.method).toBe('PATCH')
    expect(JSON.parse(String(init?.body))).toEqual({
      health_timeout_s: 10,
      service_name: 'opspilot-billing',
    })
    expect(JSON.parse(String(init?.body))).not.toHaveProperty('name')
  })

  it('carries the error body through when the backend refuses', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(409, {
          code: 'conflict',
          message: 'name billing-api is already taken',
          request_id: 'req-1',
        }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(createApplication({ name: 'billing-api' })).rejects.toMatchObject({
      status: 409,
      code: 'conflict',
    })
  })
})
