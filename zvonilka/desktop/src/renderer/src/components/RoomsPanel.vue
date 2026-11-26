<script setup lang="ts">
import RoomAudio from './RoomMeshAudio.vue'
import { ref, computed, onBeforeUnmount, onMounted } from 'vue'
import type { DirectInvite, LookupUser } from '../../../shared/auth'
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
const props = defineProps<{ userId: string; displayName: string }>()
const roomIdInput = ref('')

const presence = ref<PresenceState>({ phase: 'idle' })

const callActive = computed(() => ['connecting', 'online'].includes(presence.value.phase))
const iceMode = ref<'all' | 'relay'>('all')
const inviteDialog = ref<HTMLDialogElement | null>(null)
const invitationFeedback = ref<string | null>(null)

async function copyInvitation(invitation: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(invitation)
    invitationFeedback.value = 'Приглашение скопировано.'
  } catch {
    invitationFeedback.value = 'Не удалось скопировать. Выделите текст приглашения вручную.'
  }
}

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

const targetUsername = ref('')
const foundUser = ref<LookupUser | null>(null)
const incoming = ref<DirectInvite[]>([])

function findUser(): Promise<void> {
  foundUser.value = null
  invitationFeedback.value = null
  return run(
    () => window.api.lookupUser(targetUsername.value),
    (user) => {
      foundUser.value = user
    }
  )
}

function sendInvite(): Promise<void> | undefined {
  const room = ownedRoom.value
  const user = foundUser.value
  if (!room || !user) return
  invitationFeedback.value = null
  return run(
    () => window.api.sendDirectInvite(room.room_id, user.username),
    () => {
      invitationFeedback.value = `Приглашение отправлено @${user.username}.`
      foundUser.value = null
      targetUsername.value = ''
    }
  )
}

function loadInbox(): Promise<void> {
  return run(window.api.directInbox, (items) => {
    incoming.value = items
  })
}

function acceptInvite(invite: DirectInvite): Promise<void> {
  return run(
    () => window.api.acceptDirectInvite(invite.room_id, invite.id),
    (room) => {
      joinedRoom.value = room
      clearPresenceNotice()
      incoming.value = incoming.value.filter((item) => item.id !== invite.id)
    }
  )
}

function declineInvite(invite: DirectInvite): Promise<void> {
  return run(
    () => window.api.declineDirectInvite(invite.room_id, invite.id),
    () => {
      incoming.value = incoming.value.filter((item) => item.id !== invite.id)
    }
  )
}

onMounted(() => {
  void loadInbox()
})

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
  <h2 id="rooms-title">{{ callActive ? 'Звонок' : 'Комнаты' }}</h2>
  <section class="rooms" aria-labelledby="rooms-title" :aria-busy="pending">
    <template v-if="!callActive">
      <div v-if="ownedRoom || joinedRoom" class="ready-grid">
        <section v-if="ownedRoom" class="room-card" aria-label="Своя комната">
          <h3>Своя комната</h3>
          <button
            class="primary"
            type="button"
            :disabled="pending"
            @click="connect(ownedRoom.room_id)"
          >
            Войти в звонок
          </button>
          <div class="actions">
            <button type="button" @click="inviteDialog?.showModal()">Пригласить</button>
          </div>
          <details>
            <summary>Управление комнатой</summary>
            <code>{{ ownedRoom.room_id }}</code>
            <button type="button" :disabled="pending" @click="closeOwnedRoom">
              Закрыть комнату
            </button>
          </details>
        </section>
        <section v-if="joinedRoom" class="room-card" aria-label="Доступная комната">
          <h3>Доступная комната</h3>
          <code>{{ joinedRoom.room_id }}</code>
          <button
            class="primary"
            type="button"
            :disabled="pending"
            @click="connect(joinedRoom.room_id)"
          >
            Войти в звонок
          </button>
        </section>
      </div>
      <label v-if="ownedRoom || joinedRoom" class="route-setting">
        Маршрут аудио
        <select v-model="iceMode" :disabled="pending">
          <option value="all">Авто</option>
          <option value="relay">Только TURN</option>
        </select>
      </label>
      <details
        class="room-card"
        @toggle="($event.target as HTMLDetailsElement).open && loadInbox()"
      >
        <summary>Входящие приглашения ({{ incoming.length }})</summary>
        <button type="button" :disabled="pending" @click="loadInbox">Обновить</button>
        <p v-if="!incoming.length && !pending">Нет новых приглашений.</p>
        <div v-for="invite in incoming" :key="invite.id" class="invite-item">
          <p>@{{ invite.sender_username }} приглашает вас в комнату.</p>
          <div class="actions">
            <button type="button" :disabled="pending" @click="acceptInvite(invite)">Принять</button>
            <button type="button" :disabled="pending" @click="declineInvite(invite)">
              Отклонить
            </button>
          </div>
        </div>
      </details>
      <div class="lobby-grid">
        <section v-if="!ownedRoom" class="room-card" aria-label="Создание комнаты">
          <h3>Своя комната</h3>
          <p>Создайте комнату, чтобы пригласить собеседника.</p>
          <button class="primary" type="button" :disabled="pending" @click="create">
            Создать комнату
          </button>
        </section>
        <section class="room-card" aria-label="Вход по приглашению">
          <h3>Войти по приглашению</h3>
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
          <details>
            <summary>Открыть по ID</summary>
            <form @submit.prevent="openRoom">
              <label
                >ID комнаты
                <input v-model="roomIdInput" :disabled="pending" maxlength="64" required
              /></label>
              <button type="submit" :disabled="pending">Открыть комнату</button>
            </form>
          </details>
        </section>
      </div>
    </template>

    <section v-show="callActive" class="room-card call-stage" aria-label="Звонок">
      <header class="call-header">
        <div>
          <h3>{{ presence.phase === 'online' ? 'В комнате' : 'Подключаемся…' }}</h3>
          <p v-if="presence.phase === 'online'" role="status">
            {{
              presence.userIds.length === 1
                ? 'Ожидаем собеседника'
                : `Онлайн: ${presence.userIds.length}`
            }}
          </p>
          <p class="route-label">Режим: {{ iceMode === 'relay' ? 'Только TURN' : 'Авто' }}</p>
        </div>
        <div class="call-header-actions">
          <button
            v-if="
              presence.phase === 'online' &&
              ownedRoom &&
              'roomId' in presence &&
              presence.roomId === ownedRoom.room_id
            "
            type="button"
            @click="inviteDialog?.showModal()"
          >
            Пригласить
          </button>
          <button class="disconnect call-disconnect" type="button" @click="disconnect">
            {{ presence.phase === 'connecting' ? 'Отменить' : 'Выйти из звонка' }}
          </button>
        </div>
      </header>
      <RoomAudio :display-name="props.displayName" :ice-mode="iceMode" />
    </section>

    <dialog
      ref="inviteDialog"
      class="invite-dialog"
      aria-labelledby="invite-title"
      @close="invitationFeedback = null"
    >
      <header class="call-header">
        <h3 id="invite-title">Приглашения</h3>
        <form method="dialog">
          <button class="dialog-close" type="submit" aria-label="Закрыть">
            <span aria-hidden="true">×</span>
          </button>
        </form>
      </header>
      <template v-if="ownedRoom">
        <form @submit.prevent="findUser">
          <label>
            Имя пользователя целиком
            <input
              v-model="targetUsername"
              maxlength="64"
              required
              :disabled="pending"
              @input="foundUser = null"
            />
          </label>
          <button type="submit" :disabled="pending">Найти</button>
        </form>
        <div v-if="foundUser" class="invite-item">
          <p>{{ foundUser.display_name }} · @{{ foundUser.username }}</p>
          <button type="button" :disabled="pending" @click="sendInvite">
            Отправить приглашение
          </button>
        </div>
        <button v-if="ownedRoom.memberCount < 3" type="button" :disabled="pending" @click="issue">
          Выпустить приглашение
        </button>
        <p v-else>Все места в комнате заняты.</p>
        <p v-if="pending" role="status">Выполняется запрос…</p>
        <p v-if="error" class="error" role="alert">{{ error }}</p>
        <div
          v-for="(invitation, index) in invitationsForOwnedRoom"
          :key="invitation"
          class="invite-item"
        >
          <label>Приглашение {{ index + 1 }} <input :value="invitation" readonly /></label>
          <button type="button" @click="copyInvitation(invitation)">Скопировать</button>
        </div>
        <p v-if="invitationFeedback" role="status">{{ invitationFeedback }}</p>
      </template>
    </dialog>
    <p v-if="presenceNotice" class="presence-notice" role="status">{{ presenceNotice }}</p>
    <p v-if="status" role="status">{{ status }}</p>
    <p v-if="pending" role="status">Выполняется запрос…</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped src="./RoomsPanel.css"></style>
