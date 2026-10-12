import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  acceptHostKey,
  checkServer,
  createCredential,
  createServer,
  discoverHostKey,
  listCredentials,
  listServers,
} from '@/api/inventory'

function jsonResponse(status: number, body: unknown) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const SERVER = {
  id: 7,
  name: 'web-1',
  hostname: '10.0.0.11',
  port: 22,
  ssh_user: 'opstest',
  credential_id: 3,
  description: '',
  tags: null,
  status: 'active',
  host_key_algorithm: '',
  host_key_fingerprint: '',
  host_key_state: 'unverified',
  host_key_accepted_at: null,
  last_check_at: null,
  last_check_status: 'unknown',
  last_check_detail: '',
  groups: [],
  latest_facts: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

describe('inventory api', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('lists servers against the versioned path', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(200, [SERVER]),
    )
    vi.stubGlobal('fetch', fetchMock)

    const rows = await listServers()
    expect(rows).toHaveLength(1)
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/servers')
  })

  it('creates a server with the expected body', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(201, SERVER),
    )
    vi.stubGlobal('fetch', fetchMock)

    await createServer({
      name: 'web-1',
      hostname: '10.0.0.11',
      port: 22,
      ssh_user: 'opstest',
      credential_id: 3,
      description: '',
    })

    const init = fetchMock.mock.calls[0]?.[1]
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toEqual({
      name: 'web-1',
      hostname: '10.0.0.11',
      port: 22,
      ssh_user: 'opstest',
      credential_id: 3,
      description: '',
    })
  })

  it('omits optional fields when they are not supplied', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(201, SERVER),
    )
    vi.stubGlobal('fetch', fetchMock)

    await createServer({ name: 'web-1', hostname: '10.0.0.11', port: 22, ssh_user: 'opstest' })

    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      name: 'web-1',
      hostname: '10.0.0.11',
      port: 22,
      ssh_user: 'opstest',
    })
  })

  it('sends the confirmed fingerprint when accepting a host key', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(200, SERVER),
    )
    vi.stubGlobal('fetch', fetchMock)

    const fp = 'SHA256:' + 'A'.repeat(43)
    await acceptHostKey(7, fp)

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/servers/7/accept-hostkey')
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({ fingerprint: fp })
  })

  it('routes discovery and check through the same server id', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(200, {
          algorithm: 'ssh-ed25519',
          fingerprint: 'SHA256:' + 'B'.repeat(43),
          state: 'pending_confirm',
        }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await discoverHostKey(11)
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/servers/11/discover-hostkey')

    await checkServer(11)
    expect(fetchMock.mock.calls[1]?.[0]).toBe('/api/v1/servers/11/check')
    expect(fetchMock.mock.calls[1]?.[1]?.method).toBe('POST')
  })

  it('never puts key material in the credentials list response type', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(200, [
          {
            id: 3,
            name: 'deploy-key',
            kind: 'ssh_key',
            description: '',
            status: 'active',
            secret_ref: 'cred:3',
            fingerprint: 'SHA256:' + 'C'.repeat(32),
            meta: null,
            used_at: null,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
          },
        ]),
    )
    vi.stubGlobal('fetch', fetchMock)

    const rows = await listCredentials()
    expect(rows[0]?.secret_ref).toBe('cred:3')
    expect(Object.keys(rows[0] ?? {})).not.toContain('payload_encrypted')
    expect(Object.keys(rows[0] ?? {})).not.toContain('secret')
  })

  it('posts the secret exactly once, and the response does not echo it', async () => {
    const secret = '-----BEGIN OPENSSH PRIVATE KEY-----\nMATERIAL\n-----END-----'
    const response = {
      id: 3,
      name: 'deploy-key',
      kind: 'ssh_key',
      description: '',
      status: 'active',
      secret_ref: 'cred:3',
      fingerprint: 'SHA256:abc',
      meta: null,
      used_at: null,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    }
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(201, response),
    )
    vi.stubGlobal('fetch', fetchMock)

    const created = await createCredential({ name: 'deploy-key', kind: 'ssh_key', secret })

    expect(fetchMock.mock.calls).toHaveLength(1)
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body)).secret).toBe(secret)
    expect(JSON.stringify(created)).not.toContain('MATERIAL')
  })

  it('rejects a failed create with the typed ApiError shape', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(422, {
          code: 'validation_error',
          message: 'request body failed validation',
          request_id: 'rid-1',
          details: { fields: ['kind'] },
        }),
      ),
    )

    await expect(
      createCredential({ name: 'x', kind: 'root_password', secret: 's' }),
    ).rejects.toMatchObject({ code: 'validation_error', status: 422 })
  })
})
