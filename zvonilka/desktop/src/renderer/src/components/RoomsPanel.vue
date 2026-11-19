<script setup lang="ts">
import { ref } from 'vue'
import type { RoomError, RoomResult, RoomSummary } from '../../../shared/auth'

const emit = defineEmits<{ 'sign-in-required': [] }>()

const ownedRoom = ref<RoomSummary | null>(null)
const joinedRoom = ref<RoomSummary | null>(null)
const issuedInvitation = ref('')
const invitationInput = ref('')
const pending = ref(false)
const error = ref('')

const messages: Record<Exclude<RoomError, 'sign_in_required'>, string> = {
  invalid_request: 'Проверьте формат приглашения или повторите операцию.',
  api_unavailable: 'API недоступен. Проверьте, что backend запущен.',
  forbidden: 'У этой учётной записи нет доступа к операции.',
  not_found: 'Комната или приглашение не найдены.',
  invite_expired: 'Срок приглашения истёк. Попросите владельца выпустить новое.',
  invite_revoked: 'Приглашение отозвано.',
  invite_consumed: 'Приглашение уже использовано.',
  rate_limited: 'Слишком много запросов. Подождите и попробуйте снова.',
  conflict: 'Операция недоступна в текущем состоянии комнаты.',
  unexpected: 'Не удалось выполнить операцию. Попробуйте снова.',
  owner_cannot_redeem: 'Вы уже владелец этой комнаты. Передайте приглашение другому участнику.'
}

async function run<T>(
  action: () => Promise<RoomResult<T>>,
  onSuccess: (value: T) => void
): Promise<void> {
  if (pending.value) return

  pending.value = true
  error.value = ''

  try {
    const result = await action()
    if (result.ok) {
      onSuccess(result.value)
    } else if (result.error === 'sign_in_required') {
      emit('sign-in-required')
    } else {
      error.value = messages[result.error]
    }
  } catch {
    error.value = messages.unexpected
  } finally {
    pending.value = false
  }
}

function create(): Promise<void> {
  return run(window.api.createRoom, (room) => {
    ownedRoom.value = room
    issuedInvitation.value = ''
  })
}

async function issue(): Promise<void> {
  const roomId = ownedRoom.value?.room_id
  if (!roomId) return

  await run(
    () => window.api.issueInvitation(roomId),
    (invitation) => {
      issuedInvitation.value = invitation
    }
  )
}

function redeem(): Promise<void> {
  return run(
    () => window.api.redeemInvitation(invitationInput.value),
    (room) => {
      joinedRoom.value = room
      invitationInput.value = ''
    }
  )
}
</script>

<template>
  <section class="rooms" aria-labelledby="rooms-title" :aria-busy="pending">
    <h2 id="rooms-title">Комнаты</h2>

    <button type="button" :disabled="pending || ownedRoom !== null" @click="create">
      Создать комнату
    </button>

    <template v-if="ownedRoom">
      <p>
        Созданная комната: <code>{{ ownedRoom.room_id }}</code>
      </p>
      <button type="button" :disabled="pending" @click="issue">Выпустить приглашение</button>

      <label v-if="issuedInvitation">
        Приглашение — скопируйте и передайте второму участнику
        <input :value="issuedInvitation" readonly />
      </label>
    </template>

    <form @submit.prevent="redeem">
      <label>
        Приглашение от владельца
        <input
          v-model="invitationInput"
          :disabled="pending"
          autocomplete="off"
          :spellcheck="false"
          maxlength="256"
          required
        />
      </label>
      <button type="submit" :disabled="pending">Принять приглашение</button>
    </form>

    <p v-if="joinedRoom" role="status">
      Приглашение принято. Комната: <code>{{ joinedRoom.room_id }}</code>
    </p>
    <p v-if="pending" role="status">Выполняется запрос…</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
.rooms,
form,
label {
  display: grid;
  gap: 0.5rem;
}

.rooms,
form {
  margin-top: 1rem;
}

input,
button {
  min-width: 0;
  padding: 0.5rem;
  font: inherit;
}

code,
input {
  user-select: text;
}

code {
  overflow-wrap: anywhere;
}

.error {
  color: #ffb4ab;
}
</style>
