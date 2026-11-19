<script setup lang="ts">
import { ref, computed } from 'vue'
import type { RoomError, RoomResult, RoomSummary } from '../../../shared/auth'

const emit = defineEmits<{ 'sign-in-required': [] }>()

const ownedRoom = ref<RoomSummary | null>(null)
const joinedRoom = ref<RoomSummary | null>(null)
const invitationsByRoom = ref<Record<string, string[]>>({})
const invitationsForOwnedRoom = computed(() => {
  const roomId = ownedRoom.value?.room_id
  return roomId ? (invitationsByRoom.value[roomId] ?? []) : []
})
const invitationInput = ref('')
const pending = ref(false)
const error = ref('')
const status = ref('')
const props = defineProps<{ userId: string }>()
const roomIdInput = ref('')

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
  owner_cannot_redeem: 'Вы уже владелец этой комнаты. Передайте приглашение другому участнику.',
  room_closed: 'Комната уже закрыта.',
  refresh_uncertain: 'Не удалось подтвердить обновление сессии. Чтобы продолжить, войдите заново.',
  invite_limit:
    'Нет свободных мест для новых приглашений: комната заполнена или места уже зарезервированы выданными ссылками.'
}

async function run<T>(
  action: () => Promise<RoomResult<T>>,
  onSuccess: (value: T) => void
): Promise<void> {
  if (pending.value) return

  pending.value = true
  error.value = ''
  status.value = ''

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
    // issuedInvitation.value = ''
  })
}

async function issue(): Promise<void> {
  const roomId = ownedRoom.value?.room_id
  if (!roomId) return

  await run(
    () => window.api.issueInvitation(roomId),
    (invitation) => {
      const previous = invitationsByRoom.value[roomId] ?? []
      invitationsByRoom.value[roomId] = [...previous, invitation]
    }
  )
}

async function closeOwnedRoom(): Promise<void> {
  const roomId = ownedRoom.value?.room_id
  if (!roomId || pending.value) return

  if (!window.confirm('Закрыть комнату? Её приглашения больше не будут работать.')) return

  await run(
    () => window.api.closeRoom(roomId),
    () => {
      ownedRoom.value = null
      delete invitationsByRoom.value[roomId]
      status.value = 'Комната закрыта.'
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

function openRoom(): Promise<void> {
  return run(
    () => window.api.getRoom(roomIdInput.value),
    (room) => {
      if (room.state === 'closed') {
        error.value = 'Комната уже закрыта.'
        return
      }

      if (room.owner_id === props.userId) {
        // if (ownedRoom.value?.room_id !== room.room_id) issuedInvitation.value = ''
        ownedRoom.value = room
        if (joinedRoom.value?.room_id === room.room_id) joinedRoom.value = null
      } else {
        joinedRoom.value = room
      }
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
      <p>Участники: {{ ownedRoom.memberCount }}/3</p>

      <button v-if="ownedRoom.memberCount < 3" type="button" :disabled="pending" @click="issue">
        Выпустить приглашение
      </button>

      <p v-else>Все места в комнате заняты.</p>

      <button type="button" :disabled="pending" @click="closeOwnedRoom">Закрыть комнату</button>

      <label v-for="(invitation, index) in invitationsForOwnedRoom" :key="invitation">
        Приглашение {{ index + 1 }}
        <input :value="invitation" readonly />
      </label>

      <p v-if="invitationsForOwnedRoom.length">
        Ссылки сохранены только в этом окне. Уже использованная ссылка не подойдёт другому аккаунту.
      </p>
    </template>

    <form @submit.prevent="openRoom">
      <label>
        ID существующей комнаты
        <input v-model="roomIdInput" :disabled="pending" maxlength="64" required />
      </label>
      <button type="submit" :disabled="pending">Открыть комнату</button>
    </form>

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

    <p v-if="status" role="status">{{ status }}</p>
    <p v-if="joinedRoom" role="status">
      Доступна комната: <code>{{ joinedRoom.room_id }}</code>
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
  user-select: text;
}
</style>
