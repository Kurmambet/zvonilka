<script setup lang="ts">
import { ref } from 'vue'
import type { LoginError, Profile, RegisterError } from '../../shared/auth'
import RoomsPanel from './components/RoomsPanel.vue'
import type { ServerProfileId, ServerProbeResult } from '../../shared/server'

const loginProfile = ref<ServerProfileId>('local')
const probeProfile = ref<ServerProfileId>('local')
const probePending = ref(false)
const probeResult = ref<ServerProbeResult | null>(null)

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

async function checkServer(id: ServerProfileId): Promise<void> {
  if (probePending.value) return

  probeProfile.value = id
  probePending.value = true
  probeResult.value = null

  try {
    probeResult.value = await window.api.probeServer(id)
  } catch {
    probeResult.value = { ok: false }
  } finally {
    probePending.value = false
  }
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
    const request = window.api.login(loginProfile.value, username.value, password.value)
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
    const request = window.api.register(
      loginProfile.value,
      username.value,
      displayName.value,
      password.value
    )
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
    <header class="panel-topbar">
      <h1>ZVonilka</h1>

      <details class="server-probe">
        <summary>Проверить API</summary>
        <div class="server-probe__buttons">
          <button
            type="button"
            aria-label="Проверить Local"
            :disabled="probePending"
            @click="checkServer('local')"
          >
            Local
          </button>
          <button
            type="button"
            aria-label="Проверить Staging"
            :disabled="probePending"
            @click="checkServer('staging')"
          >
            Staging
          </button>
        </div>

        <p class="server-probe__status" aria-live="polite">
          <template v-if="probePending">{{ probeProfile }}: проверяем…</template>
          <template v-else-if="probeResult?.ok">
            {{ probeProfile }} · {{ probeProfile === 'staging' ? 'HTTPS' : 'HTTP' }}
            {{ probeResult.latencyMs }} мс
          </template>
          <template v-else-if="probeResult">{{ probeProfile }}: API недоступен</template>
        </p>
      </details>
    </header>

    <section v-if="profile" aria-labelledby="profile-title">
      <div class="workspace-header">
        <h2 id="profile-title">{{ profile.display_name }}</h2>

        <button class="logout-button" type="button" :disabled="loggingOut" @click="logout">
          {{ loggingOut ? 'Выход…' : 'Выйти из аккаунта' }}
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
          Сервер
          <select v-model="loginProfile" :disabled="pending">
            <option value="local">Local</option>
            <option value="staging">Staging</option>
          </select>
        </label>
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

        <button class="primary" type="submit" :disabled="pending">
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

<style scoped src="./App.css"></style>
