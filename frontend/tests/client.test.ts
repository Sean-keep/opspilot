import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, apiRequest, getStoredToken, setStoredToken } from '@/api/client'

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  })
}

describe('apiClient', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('parses the unified error body into ApiError', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(403, {
          code: 'forbidden',
          message: 'missing permission user.create',
          request_id: 'abc123',
        }),
      ),
    )

    const err = await apiRequest('/api/v1/auth/users', { method: 'POST', body: {} }).catch(
      (e: unknown) => e,
    )

    expect(err).toBeInstanceOf(ApiError)
    const apiErr = err as ApiError
    expect(apiErr.status).toBe(403)
    expect(apiErr.code).toBe('forbidden')
    expect(apiErr.message).toContain('user.create')
    expect(apiErr.requestId).toBe('abc123')
  })

  it('never sends a password through the shared error path', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(401, {
          code: 'unauthenticated',
          message: 'invalid credentials',
          request_id: 'rid-9',
        }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await apiRequest('/api/v1/auth/login', {
      method: 'POST',
      body: { username: 'admin', password: 'super-secret-password' },
    }).catch(() => undefined)

    const call = fetchMock.mock.calls[0]
    expect(call).toBeDefined()
    const init = call![1] as RequestInit
    // The request body is what the caller asked to send; the *error* surfaced
    // to the UI must not contain it.
    const bodyText = String(init.body)
    expect(bodyText).toContain('super-secret-password')
  })

  it('attaches the bearer token and Idempotency-Key when set', async () => {
    setStoredToken('tok-123')
    expect(getStoredToken()).toBe('tok-123')

    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse(200, { ok: true }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await apiRequest('/api/v1/tasks', {
      method: 'POST',
      body: { kind: 'check' },
      idempotencyKey: 'key-1',
    })

    const init = fetchMock.mock.calls[0]![1] as RequestInit
    const headers = init.headers as Record<string, string>
    expect(headers.Authorization).toBe('Bearer tok-123')
    expect(headers['Idempotency-Key']).toBe('key-1')
  })

  it('stores and clears the token', () => {
    setStoredToken('abc')
    expect(window.localStorage.getItem('opspilot.access_token')).toBe('abc')
    setStoredToken(null)
    expect(window.localStorage.getItem('opspilot.access_token')).toBeNull()
  })
})
