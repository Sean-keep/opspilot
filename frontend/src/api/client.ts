/**
 * Thin fetch wrapper around the OpsPilot API.
 *
 * Responsibilities:
 *  - always send the bearer token when we have one
 *  - surface the unified `{code, message, request_id}` error body as a typed
 *    exception instead of a raw Response
 *  - never let a caller see a password, token, or key: the backend does not
 *    return them, and this client does not log request bodies either
 */
import type { ApiErrorBody } from '@/types/api'

const TOKEN_STORAGE_KEY = 'opspilot.access_token'

/** A failed API call: the parsed body, plus the HTTP status. */
export class ApiError extends Error {
  readonly code: string
  readonly status: number
  readonly requestId: string
  readonly details: Record<string, unknown>

  constructor(status: number, body: ApiErrorBody) {
    super(body.message || `HTTP ${status}`)
    this.name = 'ApiError'
    this.code = body.code || 'unknown_error'
    this.status = status
    this.requestId = body.request_id || ''
    this.details = body.details ?? {}
  }
}

/** Raised when the network itself fails — no response to parse. */
export class NetworkError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options)
    this.name = 'NetworkError'
  }
}

export function getStoredToken(): string | null {
  return window.localStorage.getItem(TOKEN_STORAGE_KEY)
}

export function setStoredToken(token: string | null): void {
  if (token === null) {
    window.localStorage.removeItem(TOKEN_STORAGE_KEY)
    return
  }
  window.localStorage.setItem(TOKEN_STORAGE_KEY, token)
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  body?: unknown
  headers?: Record<string, string>
  /** Value for the Idempotency-Key header (task creation). */
  idempotencyKey?: string
  signal?: AbortSignal
}

async function parseErrorBody(response: Response): Promise<ApiErrorBody> {
  try {
    const parsed = (await response.json()) as Partial<ApiErrorBody>
    return {
      code: parsed.code ?? 'unknown_error',
      message: parsed.message ?? response.statusText ?? 'request failed',
      request_id: parsed.request_id ?? response.headers.get('X-Request-Id') ?? '',
      details: parsed.details,
    }
  } catch {
    return {
      code: 'unknown_error',
      message: `HTTP ${response.status} ${response.statusText}`,
      request_id: response.headers.get('X-Request-Id') ?? '',
    }
  }
}

/** Perform a request against the API and return the decoded JSON body. */
export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {
    Accept: 'application/json',
    ...options.headers,
  }

  const token = getStoredToken()
  if (token) {
    headers.Authorization = `Bearer ${token}`
  }

  let body: string | undefined
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(options.body)
  }

  if (options.idempotencyKey) {
    headers['Idempotency-Key'] = options.idempotencyKey
  }

  let response: Response
  try {
    const init: RequestInit = {
      method: options.method ?? 'GET',
      headers,
      credentials: 'same-origin',
    }
    if (body !== undefined) {
      init.body = body
    }
    if (options.signal) {
      init.signal = options.signal
    }
    response = await fetch(path, init)
  } catch (cause) {
    throw new NetworkError('network request failed', { cause })
  }

  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorBody(response))
  }

  if (response.status === 204) {
    return undefined as T
  }

  const contentType = response.headers.get('Content-Type') ?? ''
  if (!contentType.includes('application/json')) {
    return (await response.text()) as T
  }
  return (await response.json()) as T
}

export const api = {
  get: <T>(path: string, options?: RequestOptions) => apiRequest<T>(path, { ...options, method: 'GET' }),
  post: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    apiRequest<T>(path, { ...options, method: 'POST', body }),
  put: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    apiRequest<T>(path, { ...options, method: 'PUT', body }),
  patch: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    apiRequest<T>(path, { ...options, method: 'PATCH', body }),
  delete: <T>(path: string, options?: RequestOptions) =>
    apiRequest<T>(path, { ...options, method: 'DELETE' }),
}
