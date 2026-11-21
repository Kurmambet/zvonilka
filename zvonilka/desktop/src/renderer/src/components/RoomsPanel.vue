<script setup lang="ts">
import { ref, computed, onBeforeUnmount } from 'vue'
import type { RoomError, RoomResult, RoomSummary, PresenceState } from '../../../shared/auth'

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

const presence = ref<PresenceState>({ phase: 'idle' })

const presenceNotice = ref('')
let presenceNoticeTimer: number | undefined

function clearPresenceNotice(): void {
  window.clearTimeout(presenceNoticeTimer)
  presenceNoticeTimer = undefined
  presenceNotice.value = ''
}

function showPresenceNotice(message: string): void {
  clearPresenceNotice()
  presenceNotice.value = message
  presenceNoticeTimer = window.setTimeout(clearPresenceNotice, 12_000)
}

const presenceMessages: Record<string, string> = {
  room_closed: 'Владелец закрыл комнату.',
  room_not_found: 'Комната не найдена.',
  not_admitted: 'У этой учётной записи нет доступа к комнате.',
  connection_replaced: 'Подключение заменено другим экземпляром приложения.',
  session_revoked: 'Сессия отозвана. Войдите снова.',
  invalid_access: 'Не удалось авторизовать подключение. Войдите снова.',
  heartbeat_timeout: 'Сервер перестал отвечать на heartbeat.',
  reauthentication_timeout: 'Не удалось вовремя обновить авторизацию подключения.',
  transport_error: 'Не удалось установить или сохранить соединение с сервером.',
  server_shutdown: 'Сервер завершил работу.',
  refresh_uncertain: messagesForRefresh()
}

function messagesForRefresh(): string {
  return 'Не удалось подтвердить обновление сессии. Войдите заново.'
}

const unsubscribePresence = window.api.onPresence((state) => {
  presence.value = state

  if (state.phase === 'connecting' || state.phase === 'idle') {
    clearPresenceNotice()
    return
  }
  if (state.phase !== 'ended') return

  if (state.reason === 'sign_in_required' || state.reason === 'session_revoked') {
    emit('sign-in-required')
    return
  }

  if (state.reason === 'room_closed') {
    if (ownedRoom.value?.room_id === state.roomId) ownedRoom.value = null
    if (joinedRoom.value?.room_id === state.roomId) joinedRoom.value = null
    delete invitationsByRoom.value[state.roomId]
  }

  showPresenceNotice(presenceMessages[state.reason] ?? 'Подключение к комнате завершено.')
})

onBeforeUnmount(() => {
  unsubscribePresence()
  clearPresenceNotice()
})

function connect(roomId: string): Promise<void> {
  return run(
    () => window.api.connectRoom(roomId),
    () => {}
  )
}

async function disconnect(): Promise<void> {
  try {
    await window.api.disconnectRoom()
  } catch {
    error.value = 'Не удалось отключиться. Повторите попытку.'
  }
}

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
    clearPresenceNotice()
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
      clearPresenceNotice()
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

      clearPresenceNotice()

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
  <h2 id="rooms-title">Комнаты</h2>
  <section class="rooms" aria-labelledby="rooms-title" :aria-busy="pending">
    <div class="room-layout">
      <section class="room-card room-tools" aria-label="Управление комнатами">
        <button type="button" :disabled="pending || ownedRoom !== null" @click="create">
          Создать комнату
        </button>

        <template v-if="ownedRoom">
          <p>
            Созданная комната: <code>{{ ownedRoom.room_id }}</code>
          </p>

          <button v-if="ownedRoom.memberCount < 3" type="button" :disabled="pending" @click="issue">
            Выпустить приглашение
          </button>

          <p v-else>Все места в комнате заняты.</p>

          <button type="button" :disabled="pending" @click="closeOwnedRoom">Закрыть комнату</button>

          <label v-for="(invitation, index) in invitationsForOwnedRoom" :key="invitation">
            Приглашение {{ index + 1 }}
            <input :value="invitation" readonly />
          </label>

          <button type="button" :disabled="pending" @click="connect(ownedRoom.room_id)">
            Подключиться к своей комнате
          </button>

          <p v-if="invitationsForOwnedRoom.length">
            Ссылки сохранены только в этом окне. Уже использованная ссылка не подойдёт другому
            аккаунту.
          </p>
        </template>

        <details>
          <summary>Открыть комнату или принять приглашение</summary>

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
        </details>
        <p v-if="status" role="status">{{ status }}</p>
        <p v-if="joinedRoom" role="status">
          Доступна комната: <code>{{ joinedRoom.room_id }}</code>
        </p>

        <button
          v-if="joinedRoom"
          type="button"
          :disabled="pending"
          @click="connect(joinedRoom.room_id)"
        >
          Подключиться к доступной комнате
        </button>
      </section>

      <section class="room-card room-presence" aria-label="Подключение к комнате">
        <h3>Подключение</h3>

        <p v-if="presenceNotice" class="presence-notice" role="status">
          {{ presenceNotice }}
        </p>

        <p v-if="presence.phase === 'connecting'" role="status">Подключаемся…</p>

        <template v-else-if="presence.phase === 'online'">
          <p>Онлайн: {{ presence.userIds.length }}</p>
          <ul class="online-list">
            <li v-for="id in presence.userIds" :key="id">
              {{ id === props.userId ? 'Вы' : id }}
            </li>
          </ul>
        </template>

        <p v-else>Нет активного подключения.</p>

        <button
          v-if="presence.phase === 'connecting' || presence.phase === 'online'"
          type="button"
          @click="disconnect"
        >
          Отключиться
        </button>
      </section>
    </div>

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

.room-layout {
  display: grid;
  grid-template-columns: minmax(0, 3fr) minmax(0, 2fr);
  align-items: start;
  gap: 0.75rem;
}

.room-card {
  display: grid;
  gap: 0.5rem;
  min-width: 0;
  padding: 0.75rem;
  border: 1px solid var(--ev-c-gray-3);
  border-radius: 0.625rem;
  background: var(--color-background-mute);
}

.room-card input {
  width: 100%;
}

.room-card summary {
  cursor: pointer;
}

.room-card form {
  margin-top: 0.625rem;
}

.presence-notice,
.online-list {
  overflow-wrap: anywhere;
  user-select: text;
}

.presence-notice {
  color: #ffd89b;
}

.online-list {
  padding: 0;
}

@media (max-width: 680px) {
  .room-layout {
    grid-template-columns: minmax(0, 1fr);
  }

  .room-presence {
    grid-row: 1;
  }
}
</style>
