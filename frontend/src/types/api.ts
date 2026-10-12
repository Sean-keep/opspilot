/** Shapes shared with the FastAPI backend. Keep in sync with backend/app. */

/** The single error body every endpoint returns on failure. */
export interface ApiErrorBody {
  code: string
  message: string
  request_id: string
  details?: Record<string, unknown> | undefined
}

export interface RoleOut {
  id: number
  name: string
  description: string
}

export interface UserOut {
  id: number
  username: string
  display_name: string
  email: string
  status: string
  roles: RoleOut[]
  permissions: string[]
  last_login_at: string | null
}

export interface TokenResponse {
  access_token: string
  token_type: string
  expires_in: number
  user: UserOut
}

export interface MeOut {
  user: UserOut
  permissions: string[]
}

export interface HealthzOut {
  status: string
  env: string
  time: string
  database: 'ok' | 'error'
}
