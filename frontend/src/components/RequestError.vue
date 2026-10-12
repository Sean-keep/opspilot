<script setup lang="ts">
/**
 * Render a failed API call in a consistent way.
 *
 * Shows the server's stable code and human message plus the request id, which
 * is what an operator pastes into a support thread. Never renders ``details``
 * blindly — a caller passes only what is safe to show.
 */
import { computed } from 'vue'

import { ApiError, NetworkError } from '@/api/client'

const props = defineProps<{
  error: unknown
}>()

const isKnown = computed(() => props.error instanceof ApiError || props.error instanceof NetworkError)

const code = computed(() => (props.error instanceof ApiError ? props.error.code : 'network_error'))
const message = computed(() => {
  if (isKnown.value && props.error instanceof Error) {
    return props.error.message
  }
  return '发生未知错误'
})
const requestId = computed(() => (props.error instanceof ApiError ? props.error.requestId : ''))
const status = computed(() => (props.error instanceof ApiError ? props.error.status : 0))
</script>

<template>
  <div class="request-error" role="alert">
    <p class="request-error-message">
      <span class="request-error-code">{{ code }}</span>
      {{ message }}
    </p>
    <p v-if="requestId" class="request-error-meta">
      request_id: <code>{{ requestId }}</code>
      <template v-if="status"> · HTTP {{ status }}</template>
    </p>
  </div>
</template>

<style scoped>
.request-error {
  padding: 12px 16px;
  background: #fef0f0;
  border: 1px solid #fbc4c4;
  border-radius: 6px;
}

.request-error-message {
  margin: 0;
  color: #f56c6c;
}

.request-error-code {
  margin-right: 8px;
  padding: 1px 6px;
  background: #fde2e2;
  border-radius: 3px;
  font-size: 12px;
  font-weight: 600;
}

.request-error-meta {
  margin: 6px 0 0;
  color: #a8abb2;
  font-size: 12px;
}
</style>
