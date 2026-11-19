<script setup lang="ts">
import { ref } from 'vue'
import type { LoginError, Profile } from '../../shared/auth'
import RoomsPanel from './components/RoomsPanel.vue'

const username = ref('')
const password = ref('')
const pending = ref(false)
const error = ref('')
const profile = ref<Profile | null>(null)

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
</script>

<template>
  <main class="panel">
    <h1>Zvonilka</h1>

    <section v-if="profile" aria-labelledby="profile-title">
      <h2 id="profile-title">Профиль</h2>
      <dl>
        <dt>Имя пользователя</dt>
        <dd>{{ profile.username }}</dd>
        <dt>Отображаемое имя</dt>
        <dd>{{ profile.display_name }}</dd>
      </dl>
      <RoomsPanel @sign-in-required="requireSignIn" />
    </section>

    <form v-else :aria-busy="pending" @submit.prevent="submit">
      <label>
        Имя пользователя
        <input
          v-model="username"
          autocomplete="username"
          maxlength="64"
          :disabled="pending"
          required
        />
      </label>

      <label>
        Пароль
        <input
          v-model="password"
          type="password"
          autocomplete="current-password"
          maxlength="128"
          :disabled="pending"
          required
        />
      </label>

      <button type="submit" :disabled="pending">
        {{ pending ? 'Вход…' : 'Войти' }}
      </button>
    </form>

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
}
</style>
