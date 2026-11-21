import { randomUUID } from 'node:crypto'
import type { AudioEvent, AudioPeer } from '../shared/audio'
import type { PresenceState, RoomResult } from '../shared/auth'
import { API_BASE_URL, isRecord } from './auth'

type Refresh = () => Promise<RoomResult<string>>

const visibleErrors = new Set([
  'room_closed',
  'room_not_found',
  'not_admitted',
  'connection_replaced',
  'session_revoked',
  'invalid_access',
  'reauthentication_timeout',
  'heartbeat_timeout',
  'server_shutdown'
])

export function connectPresence(
  roomId: string,
  accessToken: string,
  refresh: Refresh,
  publish: (state: PresenceState) => void,
  audio: (event: AudioEvent) => void
): { stop: () => void; sendAudio: (target: unknown, signal: unknown) => boolean } {
  const url = new URL(`/api/v1/rooms/${roomId}/ws`, API_BASE_URL)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'

  let stopped = false
  let cleanupSocket: (() => void) | undefined
  let retriedInitialAuth = false
  let peer: AudioPeer | null = null
  let transmit: (signal: Record<string, unknown>) => boolean = () => false

  function sendAudio(target: unknown, signal: unknown): boolean {
    if (
      !peer ||
      !isRecord(target) ||
      target.roomId !== peer.roomId ||
      target.userId !== peer.userId ||
      target.generation !== peer.generation ||
      !isRecord(signal) ||
      (signal.type !== 'offer' && signal.type !== 'answer') ||
      signal.type !== (peer.initiator ? 'offer' : 'answer') ||
      typeof signal.sdp !== 'string' ||
      !signal.sdp ||
      Buffer.byteLength(signal.sdp, 'utf8') > 16384
    )
      return false
    return transmit({
      type: signal.type,
      sdp: signal.sdp,
      target_user_id: peer.userId,
      target_generation: peer.generation
    })
  }

  function stop(): void {
    if (stopped) return
    stopped = true
    cleanupSocket?.()
    audio({ peer: null })
  }

  function end(reason: string): void {
    if (stopped) return
    stop()
    publish({ phase: 'ended', roomId, reason })
  }

  async function renew(): Promise<RoomResult<string>> {
    try {
      return await refresh()
    } catch {
      return { ok: false, error: 'unexpected' }
    }
  }

  function start(token: string): void {
    let socket: WebSocket
    try {
      socket = new WebSocket(url.href)
    } catch {
      end('transport_error')
      return
    }

    let retired = false
    let generation: string | undefined
    let selfId: string | undefined
    let interval: ReturnType<typeof setInterval> | undefined
    let heartbeatTimer: ReturnType<typeof setTimeout> | undefined
    let reauthTimer: ReturnType<typeof setTimeout> | undefined
    let heartbeatId: string | undefined
    let reauthId: string | undefined
    let refreshing = false
    const authId = randomUUID()
    const joinTimer = setTimeout(() => end('join_timeout'), 10_000)

    function retire(): void {
      retired = true
      clearTimeout(joinTimer)
      clearTimeout(heartbeatTimer)
      clearTimeout(reauthTimer)
      clearInterval(interval)
      socket.close()
    }

    cleanupSocket = retire

    function send(payload: Record<string, unknown>): boolean {
      if (stopped || retired || socket.readyState !== WebSocket.OPEN) return false
      try {
        socket.send(JSON.stringify({ version: 1, ...payload }))
        return true
      } catch {
        end('transport_error')
        return false
      }
    }

    transmit = (signal) =>
      Boolean(generation) && send({ ...signal, generation, request_id: randomUUID() })

    async function retryInitialAuth(): Promise<void> {
      retriedInitialAuth = true
      retire()
      const result = await renew()
      if (stopped) return
      if (!result.ok) {
        end(result.error)
        return
      }
      start(result.value)
    }

    async function reauthenticate(): Promise<void> {
      if (refreshing || reauthId || !generation) return
      refreshing = true
      const result = await renew()
      refreshing = false
      if (stopped || retired) return
      if (!result.ok) {
        end(result.error)
        return
      }

      const id = randomUUID()
      reauthId = id
      if (
        send({
          type: 'reauthenticate',
          request_id: id,
          generation,
          access_token: result.value
        })
      ) {
        reauthTimer = setTimeout(() => end('reauthentication_timeout'), 10_000)
      }
    }

    socket.addEventListener('open', () => {
      send({
        type: 'authenticate',
        request_id: authId,
        room_id: roomId,
        access_token: token,
        app_version: '1',
        capabilities: []
      })
    })

    socket.addEventListener('message', (event) => {
      if (stopped || retired || typeof event.data !== 'string') return

      let payload: unknown
      try {
        payload = JSON.parse(event.data)
      } catch {
        end('protocol_error')
        return
      }
      if (!isRecord(payload) || payload.version !== 1) {
        end('protocol_error')
        return
      }

      switch (payload.type) {
        case 'joined': {
          const { heartbeat_interval_ms: every, heartbeat_timeout_ms: timeout } = payload
          if (
            generation ||
            payload.request_id !== authId ||
            !isRecord(payload.room) ||
            payload.room.room_id !== roomId ||
            typeof payload.generation !== 'string' ||
            typeof payload.user_id !== 'string' ||
            typeof every !== 'number' ||
            typeof timeout !== 'number' ||
            !Number.isInteger(every) ||
            !Number.isInteger(timeout) ||
            every < 1 ||
            timeout <= every ||
            timeout > 2_147_483_647
          ) {
            end('protocol_error')
            return
          }

          generation = payload.generation
          selfId = payload.user_id
          clearTimeout(joinTimer)
          publish({ phase: 'online', roomId, userIds: [payload.user_id] })

          interval = setInterval(() => {
            if (heartbeatId) return
            const id = randomUUID()
            heartbeatId = id
            if (send({ type: 'heartbeat', request_id: id, generation })) {
              heartbeatTimer = setTimeout(() => end('heartbeat_timeout'), timeout)
            }
          }, every)
          break
        }

        case 'participants': {
          if (!generation || payload.room_id !== roomId || !Array.isArray(payload.members)) return
          const userIds: string[] = []
          for (const member of payload.members) {
            if (!isRecord(member) || typeof member.user_id !== 'string') {
              end('protocol_error')
              return
            }
            userIds.push(member.user_id)
          }
          const other = payload.members.find(
            (member: unknown) => isRecord(member) && member.user_id !== selfId
          ) as unknown
          const next =
            userIds.length === 2 &&
            selfId &&
            isRecord(other) &&
            typeof other.user_id === 'string' &&
            typeof other.generation === 'string'
              ? {
                  roomId,
                  userId: other.user_id,
                  generation: other.generation,
                  initiator: selfId < other.user_id
                }
              : null
          if (peer?.userId !== next?.userId || peer?.generation !== next?.generation) {
            peer = next
            audio({ peer })
          }
          publish({ phase: 'online', roomId, userIds })
          break
        }

        case 'offer':
        case 'answer':
          if (
            peer &&
            payload.room_id === roomId &&
            payload.target_generation === generation &&
            payload.from_user_id === peer.userId &&
            payload.from_generation === peer.generation &&
            payload.type === (peer.initiator ? 'answer' : 'offer') &&
            typeof payload.sdp === 'string' &&
            Buffer.byteLength(payload.sdp, 'utf8') <= 16384
          )
            audio({ peer, signal: { type: payload.type, sdp: payload.sdp } })
          break

        case 'heartbeat_ack':
          if (
            heartbeatId &&
            payload.request_id === heartbeatId &&
            payload.generation === generation
          ) {
            clearTimeout(heartbeatTimer)
            heartbeatId = undefined
          }
          break

        case 'reauth_required':
          if (generation && payload.generation === generation) void reauthenticate()
          break

        case 'reauthenticated':
          if (reauthId && payload.request_id === reauthId && payload.generation === generation) {
            clearTimeout(reauthTimer)
            reauthId = undefined
          }
          break

        case 'error':
          if (payload.fatal !== true) break
          if (!generation && payload.code === 'invalid_access' && !retriedInitialAuth) {
            void retryInitialAuth()
          } else {
            const code = payload.code
            end(typeof code === 'string' && visibleErrors.has(code) ? code : 'protocol_error')
          }
          break
      }
    })

    socket.addEventListener('error', () => {
      if (!retired) end('transport_error')
    })
    socket.addEventListener('close', () => {
      if (!retired) end('disconnected')
    })
  }

  publish({ phase: 'connecting', roomId })
  start(accessToken)
  return { stop, sendAudio }
}
