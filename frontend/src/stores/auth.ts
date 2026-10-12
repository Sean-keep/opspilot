/**
 * Authentication and permission state.
 *
 * The permission list here is a **UX affordance**, not a security control: it
 * decides whether a menu item is visible. Every privileged action is enforced
 * again by the backend, which is the only real boundary.
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api, ApiError, getStoredToken, setStoredToken } from '@/api/client'
import type { MeOut, TokenResponse, UserOut } from '@/types/api'

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(getStoredToken())
  const user = ref<UserOut | null>(null)
  const permissions = ref<string[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)

  const isAuthenticated = computed(() => token.value !== null && user.value !== null)
  const username = computed(() => user.value?.username ?? '')

  function hasPermission(code: string): boolean {
    return permissions.value.includes(code)
  }

  function hasAnyPermission(codes: readonly string[]): boolean {
    return codes.some((code) => permissions.value.includes(code))
  }

  async function login(usernameInput: string, password: string): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const response = await api.post<TokenResponse>('/api/v1/auth/login', {
        username: usernameInput,
        password,
      })
      token.value = response.access_token
      setStoredToken(response.access_token)
      user.value = response.user
      permissions.value = response.user.permissions
    } catch (cause) {
      // Never echo a password back; surface only the server's message.
      if (cause instanceof ApiError) {
        error.value = cause.message
      } else {
        error.value = 'login failed'
      }
      throw cause
    } finally {
      loading.value = false
    }
  }

  /** Re-read identity and permissions. Call after a page load or a role change. */
  async function refresh(): Promise<void> {
    if (token.value === null) {
      return
    }
    try {
      const me = await api.get<MeOut>('/api/v1/auth/me')
      user.value = me.user
      permissions.value = me.permissions
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 401) {
        logout()
        return
      }
      throw cause
    }
  }

  function logout(): void {
    token.value = null
    user.value = null
    permissions.value = []
    error.value = null
    setStoredToken(null)
  }

  return {
    token,
    user,
    permissions,
    loading,
    error,
    isAuthenticated,
    username,
    hasPermission,
    hasAnyPermission,
    login,
    refresh,
    logout,
  }
})
