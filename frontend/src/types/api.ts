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

// ---------------------------------------------------------------------------
// S1: inventory
// ---------------------------------------------------------------------------

/** One collected snapshot of system facts. Append-only on the server. */
export interface FactsOut {
  id: number
  server_id: number
  collected_at: string
  os_name: string
  os_version: string
  kernel: string
  arch: string
  cpu_count: number
  cpu_model: string
  memory_total_mb: number
  memory_available_mb: number
  disk_total_gb: number
  disk_available_gb: number
}

export type HostKeyState = 'unverified' | 'pending_confirm' | 'verified' | 'mismatch'
export type ServerStatus = 'active' | 'disabled' | 'deleted'
export type CheckStatus = 'ok' | 'failed' | 'unknown'

/**
 * A managed host as the API returns it.
 *
 * There is deliberately no credential field for key material — only the opaque
 * `credential_id` reference. See docs/security-model.md.
 */
export interface ServerOut {
  id: number
  name: string
  hostname: string
  port: number
  ssh_user: string
  credential_id: number | null
  description: string
  tags: Record<string, unknown> | null
  status: ServerStatus

  host_key_algorithm: string
  host_key_fingerprint: string
  host_key_state: HostKeyState
  host_key_accepted_at: string | null

  last_check_at: string | null
  last_check_status: CheckStatus
  last_check_detail: string

  groups: { id: number; name: string; description: string }[]
  latest_facts: FactsOut | null
  created_at: string
  updated_at: string
}

export interface ServerCreateBody {
  name: string
  hostname: string
  port: number
  ssh_user: string
  credential_id?: number | null
  description?: string
}

export interface HostKeyDiscoverOut {
  algorithm: string
  fingerprint: string
  state: HostKeyState
}

// ---------------------------------------------------------------------------
// S2: tasks
// ---------------------------------------------------------------------------

/**
 * Task lifecycle. Terminal states are `SUCCESS`, `PARTIAL_SUCCESS`, `FAILED`
 * and `CANCELLED`; `SUCCESS` is hard-terminal and cannot move again.
 */
export type TaskStatus =
  | 'PENDING'
  | 'QUEUED'
  | 'RUNNING'
  | 'SUCCESS'
  | 'PARTIAL_SUCCESS'
  | 'FAILED'
  | 'CANCELLING'
  | 'CANCELLED'

/** Per-server outcome. `UNKNOWN` is human-resolved and never auto-retried. */
export type RunStatus =
  | 'WAITING'
  | 'PRECHECK'
  | 'EXECUTING'
  | 'VERIFYING'
  | 'SUCCESS'
  | 'FAILED'
  | 'CANCELLING'
  | 'CANCELLED'
  | 'UNKNOWN'

export type StepStatus =
  | 'PENDING'
  | 'RUNNING'
  | 'SUCCESS'
  | 'FAILED'
  | 'SKIPPED'
  | 'CANCELLED'
  | 'UNKNOWN'

export type IdempotencyClass = 'SAFE' | 'CONDITIONAL' | 'DESTRUCTIVE'

export interface TaskStepOut {
  id: number
  step_key: string
  attempt: number
  action: string
  status: StepStatus
  idempotency_class: IdempotencyClass
  started_at: string | null
  finished_at: string | null
  exit_code: number | null
  duration_ms: number | null
  result: Record<string, unknown> | null
}

export interface TaskRunOut {
  id: number
  task_id: number
  server_id: number
  status: RunStatus
  attempt: number
  lease_owner: string
  lease_expires_at: string | null
  heartbeat_at: string | null
  started_at: string | null
  finished_at: string | null
  error_detail: string
  recovery_note: string
  steps: TaskStepOut[]
}

export interface TaskOut {
  id: number
  kind: string
  title: string
  status: TaskStatus
  idempotency_key: string
  requested_by: number | null
  requested_by_name: string
  queued_at: string | null
  started_at: string | null
  finished_at: string | null
  cancel_requested_at: string | null
  retry_of_task_id: number | null
  created_at: string
  updated_at: string
}

export interface TaskDetailOut extends TaskOut {
  runs: TaskRunOut[]
}

export interface TaskLogOut {
  id: number
  run_id: number
  step_id: number | null
  seq: number
  ts: string
  level: string
  stream: string
  message: string
}

export interface TaskLogPage {
  items: TaskLogOut[]
  next_seq: number
  total: number
}

export interface TaskCancelBody {
  reason?: string
  /** Required: the backend refuses a cancel without it. */
  confirm: true
}

export interface TaskRetryBody {
  reason?: string
  run_ids?: number[]
}

export type RunVerdict = 'success' | 'retry' | 'failure'

export interface RunResolveBody {
  verdict: RunVerdict
  reason: string
}

export interface RunResolveOut {
  id: number
  task_id: number
  server_id: number
  status: RunStatus
  attempt: number
  recovery_note: string
}

// ---------------------------------------------------------------------------
// S1: credentials
// ---------------------------------------------------------------------------

/**
 * Credential metadata. Never key material — the backend has no field for it
 * and neither does this type.
 */
export interface CredentialOut {
  id: number
  name: string
  kind: 'ssh_key' | 'ssh_password' | 'sudo_password'
  description: string
  status: 'active' | 'retired'
  secret_ref: string
  fingerprint: string
  meta: Record<string, unknown> | null
  used_at: string | null
  created_at: string
  updated_at: string
}
