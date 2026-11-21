<script setup lang="ts">
import { ref } from 'vue'
import type { LoginError, Profile, RegisterError } from '../../shared/auth'
import RoomsPanel from './components/RoomsPanel.vue'

const username = ref('')
const password = ref('')
const pending = ref(false)
const error = ref('')
const profile = ref<Profile | null>(null)
const registering = ref(false)
const displayName = ref('')
const passwordConfirmation = ref('')
const notice = ref('')
const loggingOut = ref(false)

const registerMessages: Record<RegisterError, string> = {
  invalid_request: 'Проверьте имя пользователя, отображаемое имя и пароль.',
  conflict: 'Не удалось зарегистрировать это имя пользователя. Попробуйте другое.',
  rate_limited: 'Слишком много попыток регистрации. Попробуйте позже.',
  api_unavailable: 'API недоступен. Проверьте, что backend запущен.',
  unexpected: 'Не удалось подтвердить регистрацию. Попробуйте войти или повторите позже.'
}

const messages: Record<LoginError, string> = {
  invalid_credentials: 'Неверное имя пользователя или пароль.',
  invalid_request: 'Проверьте имя пользователя и пароль и повторите попытку.',
  rate_limited: 'Слишком много попыток. Подождите и попробуйте снова.',
  api_unavailable: 'API недоступен. Проверьте, что backend запущен, и попробуйте снова.',
  profile_failed: 'Не удалось получить профиль после входа. Попробуйте войти снова.',
  unexpected: 'Не удалось выполнить вход. Попробуйте снова.'
}

function requireSignIn(): void {
  profile.value = null
  error.value = 'Сеанс входа больше не действует. Войдите снова.'
}

async function submit(): Promise<void> {
  if (pending.value || profile.value) return

  pending.value = true
  error.value = ''
  notice.value = ''

  try {
    const request = window.api.login(username.value, password.value)
    password.value = ''
    const result = await request

    if (result.ok) {
      profile.value = result.profile
    } else {
      error.value = messages[result.error]
    }
  } catch {
    error.value = messages.unexpected
  } finally {
    password.value = ''
    pending.value = false
  }
}

function switchAuthMode(): void {
  if (pending.value) return

  registering.value = !registering.value
  password.value = ''
  passwordConfirmation.value = ''
  error.value = ''
  notice.value = ''
}

async function submitRegistration(): Promise<void> {
  if (pending.value || profile.value) return

  error.value = ''
  notice.value = ''

  if (password.value !== passwordConfirmation.value) {
    error.value = 'Пароли не совпадают.'
    return
  }

  pending.value = true

  try {
    const request = window.api.register(username.value, displayName.value, password.value)
    password.value = ''
    passwordConfirmation.value = ''

    const result = await request
    if (!result.ok) {
      error.value = registerMessages[result.error]
      return
    }

    username.value = result.username
    displayName.value = ''
    registering.value = false
    notice.value = 'Аккаунт создан. Введите пароль, чтобы войти.'
  } catch {
    error.value = registerMessages.unexpected
  } finally {
    password.value = ''
    passwordConfirmation.value = ''
    pending.value = false
  }
}

async function logout(): Promise<void> {
  if (loggingOut.value || !profile.value) return

  loggingOut.value = true
  error.value = ''
  notice.value = ''
  password.value = ''
  passwordConfirmation.value = ''

  try {
    const result = await window.api.logout()
    notice.value =
      result === 'not_confirmed'
        ? 'Вы вышли из приложения. Сервер не подтвердил завершение сессии.'
        : 'Вы вышли из аккаунта.'
  } catch {
    error.value = 'Не удалось выполнить выход. Повторите попытку или закройте приложение.'
  } finally {
    loggingOut.value = false
  }

  if (!error.value) {
    registering.value = false
    profile.value = null
  }
}
</script>

<template>
  <main class="panel" :class="{ 'panel--workspace': profile }">
    <h1>Zvonilka</h1>

    <section v-if="profile" aria-labelledby="profile-title">
      <div class="workspace-header">
        <h2 id="profile-title">{{ profile.display_name }}</h2>
        <button type="button" :disabled="loggingOut" @click="logout">
          {{ loggingOut ? 'Выход…' : 'Выйти' }}
        </button>
      </div>
      <RoomsPanel
        v-if="!loggingOut"
        :display-name="profile.display_name"
        :user-id="profile.id"
        @sign-in-required="requireSignIn"
      />
    </section>

    <template v-else>
      <form :aria-busy="pending" @submit.prevent="registering ? submitRegistration() : submit()">
        <label>
          Имя пользователя
          <input
            v-model="username"
            autocomplete="username"
            :minlength="registering ? 3 : 1"
            maxlength="64"
            :disabled="pending"
            required
          />
        </label>

        <label v-if="registering">
          Отображаемое имя
          <input v-model="displayName" maxlength="128" :disabled="pending" required />
        </label>

        <label>
          Пароль
          <input
            v-model="password"
            type="password"
            :autocomplete="registering ? 'new-password' : 'current-password'"
            :minlength="registering ? 12 : 1"
            maxlength="128"
            :disabled="pending"
            required
          />
        </label>

        <label v-if="registering">
          Повторите пароль
          <input
            v-model="passwordConfirmation"
            type="password"
            autocomplete="new-password"
            maxlength="128"
            :disabled="pending"
            required
          />
        </label>

        <button type="submit" :disabled="pending">
          {{ pending ? 'Подождите…' : registering ? 'Создать аккаунт' : 'Войти' }}
        </button>

        <button type="button" :disabled="pending" @click="switchAuthMode">
          {{ registering ? 'Назад ко входу' : 'Зарегистрироваться' }}
        </button>
      </form>

      <p v-if="notice" role="status">{{ notice }}</p>
    </template>

    <p v-if="error" class="error" role="alert">{{ error }}</p>
  </main>
</template>

<style scoped>
.panel {
  width: min(25rem, calc(100vw - 2rem));
  padding: 1.5rem;
  border-radius: 0.75rem;
  background: var(--color-background-soft);
  max-height: calc(100vh - 2rem);
  overflow-y: auto;
  scrollbar-width: thin;
  scrollbar-color: var(--ev-c-gray-1) var(--color-background-soft);
}

.panel--workspace {
  width: min(60rem, calc(100vw - 2rem));
  padding: clamp(0.75rem, 2vw, 1.125rem);
}

form,
label {
  display: grid;
  gap: 0.5rem;
}

form {
  margin-top: 1rem;
  gap: 0.875rem;
}

input,
button {
  padding: 0.5rem;
  font: inherit;
}

dd {
  margin-bottom: 0.625rem;
  overflow-wrap: anywhere;
  user-select: text;
}

.error {
  margin-top: 0.75rem;
  color: #ffb4ab;
  user-select: text;
}
.workspace-header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 0.375rem 0.75rem;
}

.workspace-header h2 {
  min-width: 0;
  font-size: 1rem;
  overflow-wrap: anywhere;
}
</style>
