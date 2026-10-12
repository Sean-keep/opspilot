<script setup lang="ts">
import { ElButton, ElForm, ElFormItem, ElInput, ElMessage } from 'element-plus'
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const form = reactive({ username: '', password: '' })
const submitting = ref(false)

async function onSubmit(): Promise<void> {
  if (!form.username || !form.password) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  submitting.value = true
  try {
    await auth.login(form.username, form.password)
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    await router.replace(redirect)
  } catch {
    // auth.login has already recorded a user-facing message; nothing to add
    // here beyond the form staying open so the operator can retry.
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <h1 class="login-title">OpsPilot</h1>
      <p class="login-subtitle">运维控制台</p>

      <ElForm label-position="top" @submit.prevent="onSubmit">
        <ElFormItem label="用户名">
          <ElInput
            v-model="form.username"
            name="username"
            autocomplete="username"
            placeholder="admin"
          />
        </ElFormItem>
        <ElFormItem label="密码">
          <ElInput
            v-model="form.password"
            name="password"
            type="password"
            show-password
            autocomplete="current-password"
            placeholder="••••••••"
            @keyup.enter="onSubmit"
          />
        </ElFormItem>

        <p v-if="auth.error" class="login-error" role="alert">{{ auth.error }}</p>

        <ElButton
          type="primary"
          native-type="submit"
          class="login-submit"
          :loading="submitting || auth.loading"
        >
          登录
        </ElButton>
      </ElForm>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100%;
  padding: 24px;
}

.login-card {
  width: 360px;
  padding: 32px 28px;
  background: #fff;
  border: 1px solid var(--opspilot-border);
  border-radius: 8px;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.06);
}

.login-title {
  margin: 0;
  font-size: 24px;
  font-weight: 600;
}

.login-subtitle {
  margin: 4px 0 24px;
  color: #909399;
}

.login-error {
  margin: 0 0 12px;
  color: #f56c6c;
  font-size: 13px;
}

.login-submit {
  width: 100%;
}
</style>
