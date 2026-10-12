/**
 * Inventory and credential endpoints.
 *
 * Nothing here ever sends or receives key material: credential write bodies
 * carry a `secret` that the server encrypts and never echoes, and every
 * response type is the metadata-only projection from `@/types/api`.
 */
import { api } from '@/api/client'
import type {
  CredentialOut,
  FactsOut,
  HostKeyDiscoverOut,
  ServerCreateBody,
  ServerOut,
  TaskOut,
} from '@/types/api'

// ---------------------------------------------------------------------------
// Servers
// ---------------------------------------------------------------------------

export function listServers(): Promise<ServerOut[]> {
  return api.get<ServerOut[]>('/api/v1/servers')
}

export function getServer(id: number): Promise<ServerOut> {
  return api.get<ServerOut>(`/api/v1/servers/${id}`)
}

export function createServer(body: ServerCreateBody): Promise<ServerOut> {
  return api.post<ServerOut>('/api/v1/servers', body)
}

export function deleteServer(id: number): Promise<ServerOut> {
  return api.delete<ServerOut>(`/api/v1/servers/${id}`)
}

/**
 * First contact: ask the host which key it offers.
 *
 * Trusts nothing. The operator must confirm the returned fingerprint through
 * `acceptHostKey` before any command can run against the host.
 */
export function discoverHostKey(id: number): Promise<HostKeyDiscoverOut> {
  return api.post<HostKeyDiscoverOut>(`/api/v1/servers/${id}/discover-hostkey`)
}

/** Pin a fingerprint the operator has confirmed. Rejects any other value. */
export function acceptHostKey(id: number, fingerprint: string): Promise<ServerOut> {
  return api.post<ServerOut>(`/api/v1/servers/${id}/accept-hostkey`, { fingerprint })
}

/**
 * Queue the read-only environment check and get back the task that will run
 * it. The API does not SSH anywhere: the Worker does, and the facts land on
 * the server record once the run finishes.
 */
export function checkServer(id: number): Promise<TaskOut> {
  return api.post<TaskOut>(`/api/v1/servers/${id}/check`)
}

export function listFacts(id: number, limit = 50): Promise<FactsOut[]> {
  return api.get<FactsOut[]>(`/api/v1/servers/${id}/facts?limit=${encodeURIComponent(limit)}`)
}

// ---------------------------------------------------------------------------
// Credentials
// ---------------------------------------------------------------------------

export function listCredentials(includeRetired = false): Promise<CredentialOut[]> {
  return api.get<CredentialOut[]>(
    `/api/v1/credentials?include_retired=${includeRetired ? 'true' : 'false'}`,
  )
}

export interface CredentialCreateBody {
  name: string
  kind: string
  secret: string
  description?: string
}

/** The `secret` is written once and never returned. */
export function createCredential(body: CredentialCreateBody): Promise<CredentialOut> {
  return api.post<CredentialOut>('/api/v1/credentials', body)
}

export function rotateCredential(id: number, secret: string): Promise<CredentialOut> {
  return api.post<CredentialOut>(`/api/v1/credentials/${id}/rotate`, { secret })
}

export function retireCredential(id: number): Promise<CredentialOut> {
  return api.post<CredentialOut>(`/api/v1/credentials/${id}/retire`)
}
