import { ref } from 'vue'
import type { Ref } from 'vue'

type Route = {
  local: string | null
  remote: string | null
  protocol: string | null
  relayProtocol: string | null
}

type LocalState = {
  ice: RTCIceConnectionState
  connection: RTCPeerConnectionState
  dtls: string | null
  route: Route | null
  microphoneOn: boolean
  sendingTrack: ReturnType<typeof trackState>
  receivingTrack: ReturnType<typeof trackState>
  player: { muted: boolean; paused: boolean; volume: number } | null
}

type RtpSample = {
  stream: string
  direction: 'in' | 'out'
  packets: number | null
  bytes: number | null
  lost: number | null
  jitterMs: number | null
}

type MediaSample = {
  at: string
  rttMs: number | null
  audio: RtpSample[]
}

type DiagnosticEvent = {
  at: string
  event: string
  peer?: string
  peerName?: string
  detail?: unknown
}

export type LinkDiagnostic = {
  id: string
  name: string
  closed: boolean
  polling: boolean
  statsError: boolean
  candidates: Record<string, number>
  selectedPair?: string
  lastState?: LocalState
  stateKey?: string
  samples: MediaSample[]
}

type DiagnosticSource = {
  pc: RTCPeerConnection
  sender?: RTCRtpSender
  remote?: MediaStream
  player?: HTMLAudioElement
  diagnostic: LinkDiagnostic
}

type TrackState = {
  state: MediaStreamTrackState
  enabled: boolean
  muted: boolean
}

function numberOrNull(value: unknown, scale = 1): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value * scale : null
}

function stringOrNull(value: unknown): string | null {
  return typeof value === 'string' ? value : null
}

function trackState(track: MediaStreamTrack | null | undefined): TrackState | null {
  return track ? { state: track.readyState, enabled: track.enabled, muted: track.muted } : null
}

function readRoute(stats: RTCStatsReport): {
  pairId: string | undefined
  route: Route | null
  dtls: string | null
  rttMs: number | null
} {
  const transport = [...stats.values()].find(
    (row) => row.type === 'transport' && typeof row.selectedCandidatePairId === 'string'
  )
  const pairId = stringOrNull(transport?.selectedCandidatePairId)
  const pair = pairId ? stats.get(pairId) : undefined
  const local = pair ? stats.get(pair.localCandidateId) : undefined
  const remote = pair ? stats.get(pair.remoteCandidateId) : undefined

  const route: Route | null = pair
    ? {
        local: stringOrNull(local?.candidateType),
        remote: stringOrNull(remote?.candidateType),
        protocol: stringOrNull(local?.protocol),
        relayProtocol: stringOrNull(local?.relayProtocol)
      }
    : null

  return {
    pairId: pairId ?? undefined,
    route,
    dtls: stringOrNull(transport?.dtlsState),
    rttMs: numberOrNull(pair?.currentRoundTripTime, 1000)
  }
}

function describeRoute(route: Route | null | undefined): string {
  if (!route?.local || !route.remote) return 'не определён'
  const viaTurn = route.local === 'relay' || route.remote === 'relay'

  return [
    viaTurn ? 'через TURN' : 'прямой',
    `${route.local} → ${route.remote}`,
    route.protocol,
    route.relayProtocol ? `до TURN: ${route.relayProtocol}` : null
  ]
    .filter(Boolean)
    .join(' · ')
}

function readAudio(stats: RTCStatsReport): RtpSample[] {
  const audio: RtpSample[] = []

  for (const row of stats.values()) {
    if ((row.kind ?? row.mediaType) !== 'audio' || typeof row.id !== 'string') continue
    if (row.type !== 'inbound-rtp' && row.type !== 'outbound-rtp') continue

    const incoming = row.type === 'inbound-rtp'
    audio.push({
      stream: row.id,
      direction: incoming ? 'in' : 'out',
      packets: numberOrNull(incoming ? row.packetsReceived : row.packetsSent),
      bytes: numberOrNull(incoming ? row.bytesReceived : row.bytesSent),
      lost: incoming ? numberOrNull(row.packetsLost) : null,
      jitterMs: incoming ? numberOrNull(row.jitter, 1000) : null
    })
  }

  return audio
}

function packetDelta(samples: MediaSample[], direction: RtpSample['direction']): string {
  let total = 0
  let intervals = 0

  for (let index = 1; index < samples.length; index++) {
    const before = samples[index - 1].audio.filter((row) => row.direction === direction)
    const after = samples[index].audio.filter((row) => row.direction === direction)
    if (before.length !== 1 || after.length !== 1 || before[0].stream !== after[0].stream) continue

    const start = before[0].packets
    const end = after[0].packets
    if (start === null || end === null || end < start) continue

    total += end - start
    intervals++
  }

  return intervals ? `+${total} (${intervals}/${samples.length - 1} интервалов)` : 'нет данных'
}

function milliseconds(value: number | null | undefined): string {
  return value == null ? 'нет данных' : `${Math.round(value * 10) / 10} мс`
}

const eventLabels: Record<string, string> = {
  'media.started': 'появились собеседники',
  'media.no-peers': 'собеседников больше нет',
  'peer.created': 'создано аудиосоединение',
  'peer.closed': 'аудиосоединение закрыто; состояние указано до закрытия',
  'peer.connection-state': 'состояние соединения',
  'ice.connection-state': 'состояние ICE',
  'ice.gathering-state': 'сбор кандидатов',
  'ice.selected-pair-changed': 'изменилась выбранная пара',
  'ice.candidate-error': 'ошибка сбора ICE-кандидата',
  'player.mute-changed': 'изменилось локальное заглушение',
  'microphone.changed': 'изменилось состояние микрофона',
  'microphone.replace-track-failed': 'не удалось подключить дорожку микрофона',
  'stats.read-failed': 'не удалось прочитать статистику'
}

type CallDiagnostics = {
  available: Ref<boolean>
  record: (event: string, link?: DiagnosticSource, detail?: unknown) => void
  createLink: (name?: string) => LinkDiagnostic
  closeLink: (link: DiagnosticSource) => void
  observeIce: (link: DiagnosticSource, isCurrent: () => boolean) => void
  sample: (
    link: DiagnosticSource,
    isCurrent: () => boolean,
    microphoneOn: () => boolean
  ) => Promise<void>
  routeNow: (pc: RTCPeerConnection) => Promise<string>
  reset: () => void
  format: (authorName: string, kind: 'summary' | 'json') => string
}

export function useCallDiagnostics(): CallDiagnostics {
  const available = ref(false)
  const events: DiagnosticEvent[] = []
  const links: LinkDiagnostic[] = []
  let sequence = 0
  let eventsDropped = 0
  let linksDropped = 0
  let startedAt: string | null = null

  function record(event: string, link?: DiagnosticSource, detail?: unknown): void {
    const at = new Date().toISOString()
    if (event === 'media.started') startedAt = at

    events.push({
      at,
      event,
      peer: link?.diagnostic.id,
      peerName: link?.diagnostic.name,
      detail
    })
    if (events.length > 512) {
      events.shift()
      eventsDropped++
    }
    available.value = true
  }

  function createLink(name?: string): LinkDiagnostic {
    const id = `peer-${++sequence}`
    const diagnostic: LinkDiagnostic = {
      id,
      name: name?.trim() || `Собеседник ${sequence}`,
      closed: false,
      polling: false,
      statsError: false,
      candidates: {},
      samples: []
    }
    links.push(diagnostic)
    return diagnostic
  }

  function closeLink(link: DiagnosticSource): void {
    link.diagnostic.closed = true
    record('peer.closed', link, {
      connection: link.pc.connectionState,
      ice: link.pc.iceConnectionState
    })

    const closed = links.filter((item) => item.closed)
    if (closed.length > 8) {
      links.splice(links.indexOf(closed[0]), 1)
      linksDropped++
    }
  }

  function observeIce(link: DiagnosticSource, isCurrent: () => boolean): void {
    const { pc, diagnostic } = link
    pc.addEventListener('icecandidate', ({ candidate }) => {
      if (!isCurrent() || !candidate?.type) return
      const type = candidate.type
      diagnostic.candidates[type] = (diagnostic.candidates[type] ?? 0) + 1
    })
    pc.addEventListener('icecandidateerror', ({ errorCode, url }) => {
      if (!isCurrent()) return
      record('ice.candidate-error', link, {
        code: errorCode,
        serverKind: /^(stuns?|turns?):/i.exec(url)?.[1]?.toLowerCase() ?? null
      })
    })
    pc.addEventListener('icegatheringstatechange', () => {
      if (!isCurrent()) return
      record('ice.gathering-state', link, {
        state: pc.iceGatheringState,
        candidates: { ...diagnostic.candidates }
      })
    })
    pc.addEventListener('iceconnectionstatechange', () => {
      if (isCurrent()) record('ice.connection-state', link, pc.iceConnectionState)
    })
  }

  async function sample(
    link: DiagnosticSource,
    isCurrent: () => boolean,
    microphoneOn: () => boolean
  ): Promise<void> {
    const { pc, diagnostic } = link
    if (!isCurrent() || diagnostic.polling) return
    diagnostic.polling = true

    try {
      const stats = await pc.getStats()
      if (!isCurrent()) return
      diagnostic.statsError = false

      const { pairId, route, dtls, rttMs } = readRoute(stats)
      if (diagnostic.selectedPair !== pairId) {
        diagnostic.selectedPair = pairId
        record('ice.selected-pair-changed', link, route)
      }

      const state: LocalState = {
        ice: pc.iceConnectionState,
        connection: pc.connectionState,
        dtls,
        route,
        microphoneOn: microphoneOn(),
        sendingTrack: trackState(link.sender?.track),
        receivingTrack: trackState(link.remote?.getAudioTracks()[0]),
        player: link.player
          ? { muted: link.player.muted, paused: link.player.paused, volume: link.player.volume }
          : null
      }

      const stateKey = JSON.stringify(state)
      if (diagnostic.stateKey !== stateKey) {
        diagnostic.stateKey = stateKey
        diagnostic.lastState = state
        record('media.state-changed', link, state)
      }

      diagnostic.samples.push({ at: new Date().toISOString(), rttMs, audio: readAudio(stats) })
      if (diagnostic.samples.length > 60) diagnostic.samples.shift()
    } catch {
      if (isCurrent() && !diagnostic.statsError) {
        diagnostic.statsError = true
        record('stats.read-failed', link)
      }
    } finally {
      diagnostic.polling = false
    }
  }

  async function routeNow(pc: RTCPeerConnection): Promise<string> {
    const { route } = readRoute(await pc.getStats())
    return route?.local && route.remote
      ? describeRoute(route)
      : `маршрут пока не определён (ICE: ${pc.iceConnectionState})`
  }

  function reset(): void {
    events.length = 0
    links.length = 0
    sequence = 0
    eventsDropped = 0
    linksDropped = 0
    startedAt = null
    available.value = false
  }

  function format(authorName: string, kind: 'summary' | 'json'): string {
    const author = authorName.trim() || 'Имя автора не передано'
    const exportedAt = new Date().toISOString()

    if (kind === 'json') {
      return JSON.stringify({
        format: 'zvonilka-media-diagnostics-v2',
        author,
        startedAt,
        exportedAt,
        userAgent: navigator.userAgent,
        diagnosticEventsDropped: eventsDropped,
        diagnosticLinksDropped: linksDropped,
        events,
        links: links.map((link) => ({
          id: link.id,
          name: link.name,
          closed: link.closed,
          gatheredCandidates: link.candidates,
          lastState: link.lastState ?? null,
          samples: link.samples
        }))
      })
    }

    const lines = [
      `Отчёт клиента: ${author}`,
      `Начало наблюдения: ${startedAt ?? 'нет данных'}`,
      `Экспорт: ${exportedAt}`
    ]

    for (const link of links) {
      const last = link.samples.at(-1)
      const incoming = last?.audio.find((row) => row.direction === 'in')
      const candidates = Object.entries(link.candidates)
        .map(([type, count]) => `${type}=${count}`)
        .join(', ')

      lines.push(
        '',
        `${link.name} (${link.id}) — ${link.closed ? 'закрыто' : 'ещё не закрыто'}`,
        `  Последний маршрут: ${describeRoute(link.lastState?.route)}`,
        `  Собранные кандидаты: ${candidates || 'нет данных'}`,
        `  Окно RTP: ${link.samples[0]?.at ?? '—'} → ${last?.at ?? '—'}`,
        `  Пакеты: принято ${packetDelta(link.samples, 'in')}; отправлено ${packetDelta(link.samples, 'out')}`,
        `  Последний RTT: ${milliseconds(last?.rttMs)}; jitter: ${milliseconds(incoming?.jitterMs)}`
      )
    }

    const recent = events.filter((item) => item.event !== 'media.state-changed').slice(-12)
    if (recent.length) {
      lines.push('', 'Последние события:')
      for (const item of recent) {
        const peer = item.peerName ? `${item.peerName} (${item.peer}): ` : ''
        const detail = item.detail === undefined ? '' : ` · ${JSON.stringify(item.detail)}`
        lines.push(`${item.at} — ${peer}${eventLabels[item.event] ?? item.event}${detail}`)
      }
    }

    if (eventsDropped || linksDropped) {
      lines.push('', `Вытеснено: событий ${eventsDropped}, соединений ${linksDropped}.`)
    }

    return lines.join('\n')
  }

  return { available, record, createLink, closeLink, observeIce, sample, routeNow, reset, format }
}
