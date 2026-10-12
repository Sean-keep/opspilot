<script setup lang="ts">
/**
 * Render children only when the caller holds the permission.
 *
 * This is a convenience for the view layer. It is deliberately *not* the
 * security model: hiding a control is not access control, and the backend
 * enforces the same rule server-side. See docs/security-model.md.
 */
import { computed, useSlots } from 'vue'

import { useAuthStore } from '@/stores/auth'

const props = withDefaults(
  defineProps<{
    /** Permission code, e.g. ``"task.create"``. */
    permission: string
    /** Require every listed code instead of just ``permission``. */
    all?: readonly string[]
  }>(),
  { all: () => [] },
)

const slots = useSlots()
const auth = useAuthStore()

const allowed = computed(() => {
  const codes = props.all.length > 0 ? props.all : [props.permission]
  return codes.every((code) => auth.hasPermission(code))
})
</script>

<template>
  <slot v-if="allowed && slots.default" />
  <slot v-else name="fallback" />
</template>
