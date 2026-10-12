/**
 * Application and environment endpoints (S4).
 *
 * An application is a definition; nothing here runs anything on a host. The
 * first command is issued when someone submits a deployment — see
 * `@/api/deployments`.
 *
 * There is no delete: deployment records refer to an application by id for as
 * long as they exist, and a record whose application has vanished cannot
 * answer "what is live on production right now".
 */
import { api } from '@/api/client'
import type {
  AppEnvironmentCreateBody,
  AppEnvironmentOut,
  AppEnvironmentUpdateBody,
  ApplicationCreateBody,
  ApplicationOut,
  ApplicationUpdateBody,
} from '@/types/api'

export function listApplications(): Promise<ApplicationOut[]> {
  return api.get<ApplicationOut[]>('/api/v1/applications')
}

export function getApplication(id: number): Promise<ApplicationOut> {
  return api.get<ApplicationOut>(`/api/v1/applications/${id}`)
}

export function createApplication(body: ApplicationCreateBody): Promise<ApplicationOut> {
  return api.post<ApplicationOut>('/api/v1/applications', body)
}

export function updateApplication(
  id: number,
  body: ApplicationUpdateBody,
): Promise<ApplicationOut> {
  return api.patch<ApplicationOut>(`/api/v1/applications/${id}`, body)
}

export function createEnvironment(
  applicationId: number,
  body: AppEnvironmentCreateBody,
): Promise<AppEnvironmentOut> {
  return api.post<AppEnvironmentOut>(`/api/v1/applications/${applicationId}/environments`, body)
}

/**
 * `name` is not editable on purpose: an environment's name is its identity
 * within the application, and every deployment record that ever pointed at it
 * refers to that name.
 */
export function updateEnvironment(
  applicationId: number,
  environmentId: number,
  body: AppEnvironmentUpdateBody,
): Promise<AppEnvironmentOut> {
  return api.patch<AppEnvironmentOut>(
    `/api/v1/applications/${applicationId}/environments/${environmentId}`,
    body,
  )
}
