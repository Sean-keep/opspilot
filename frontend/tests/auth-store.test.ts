import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import { useAuthStore } from '@/stores/auth'

describe('auth store permissions', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    window.localStorage.clear()
  })

  it('starts logged out with no permissions', () => {
    const auth = useAuthStore()
    expect(auth.isAuthenticated).toBe(false)
    expect(auth.hasPermission('task.create')).toBe(false)
  })

  it('treats the permission list as a UX hint, not a gate', () => {
    const auth = useAuthStore()
    // Even with the permission, the backend is what says yes or no — this
    // helper only decides whether a control is rendered.
    auth.permissions = ['task.read']
    expect(auth.hasPermission('task.read')).toBe(true)
    expect(auth.hasPermission('task.create')).toBe(false)
    expect(auth.hasAnyPermission(['task.create', 'task.read'])).toBe(true)
    expect(auth.hasAnyPermission(['task.create', 'task.cancel'])).toBe(false)
  })

  it('clears identity on logout', () => {
    const auth = useAuthStore()
    auth.token = 'tok'
    auth.permissions = ['task.read']
    auth.logout()
    expect(auth.token).toBeNull()
    expect(auth.isAuthenticated).toBe(false)
    expect(auth.permissions).toEqual([])
  })
})
