/**
 * Deployment endpoints (S4): submit a pinned release, read the history, roll
 * back.
 *
 * Both submit and rollback return **202** with the *record*, not the result:
 * nothing has been installed yet. The work is queued for the Worker, and the
 * linked task is where the per-server steps and logs appear.
 *
 * `idempotencyKey` makes a retry safe — the original deployment comes back
 * instead of a second one being queued. Rollback refuses without
 * `confirm: true`; that is the backend's rule, not a UI convention.
 */
import { api } from '@/api/client'
import type {
  DeployCreateBody,
  DeploymentOut,
  RollbackCreateBody,
} from '@/types/api'

export function listDeployments(options?: {
  applicationId?: number
  limit?: number
  offset?: number
}): Promise<DeploymentOut[]> {
  const params = new URLSearchParams()
  if (options?.applicationId !== undefined) {
    params.set('application_id', String(options.applicationId))
  }
  params.set('limit', String(options?.limit ?? 50))
  if (options?.offset !== undefined) {
    params.set('offset', String(options.offset))
  }
  return api.get<DeploymentOut[]>(`/api/v1/deployments?${params.toString()}`)
}

export function listApplicationDeployments(
  applicationId: number,
  limit = 50,
): Promise<DeploymentOut[]> {
  return api.get<DeploymentOut[]>(
    `/api/v1/applications/${applicationId}/deployments?limit=${encodeURIComponent(limit)}`,
  )
}

export function getDeployment(id: number): Promise<DeploymentOut> {
  return api.get<DeploymentOut>(`/api/v1/deployments/${id}`)
}

/**
 * Queue one pinned release for one environment of one application.
 *
 * `exactOptionalPropertyTypes` means optional fields must be omitted rather
 * than passed as `undefined`, so the body is assembled with only what was set.
 */
export function deployRelease(
  applicationId: number,
  body: DeployCreateBody,
  idempotencyKey?: string,
): Promise<DeploymentOut> {
  return api.post<DeploymentOut>(
    `/api/v1/applications/${applicationId}/deploy`,
    body,
    idempotencyKey === undefined ? {} : { idempotencyKey },
  )
}

/**
 * Put a previous release back, as a **new** deployment. The response is the
 * new record; the one being undone keeps its own status so history can say
 * what happened, not only what the operator did next.
 */
export function rollbackDeployment(
  deploymentId: number,
  body: RollbackCreateBody,
  idempotencyKey?: string,
): Promise<DeploymentOut> {
  return api.post<DeploymentOut>(
    `/api/v1/deployments/${deploymentId}/rollback`,
    body,
    idempotencyKey === undefined ? {} : { idempotencyKey },
  )
}
