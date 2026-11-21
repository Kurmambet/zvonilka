<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'

import type { ComponentPublicInstance } from 'vue'
import type { AudioPeer, AudioSignal } from '../../../shared/audio'

type Link = {
  peer: AudioPeer
  pc: RTCPeerConnection
  sender?: RTCRtpSender
  remote?: MediaStream
  player?: HTMLAudioElement
  cancelGather?: () => void
  diagnostic: LinkDiagnostic
}

const links = new Map<string, Link>()
const peers = shallowRef<AudioPeer[]>([])
const notes = ref<Record<string, string>>({})
const microphoneError = ref('')
const microphone = ref(false)
const busy = ref(false)
let capture: MediaStream | undefined
let ticket = 0

const microphones = shallowRef<MediaDeviceInfo[]>([])
const selectedMicrophone = ref('')
const testing = ref(false)
const level = ref(0)

let disposed = false
let deviceRequest = 0
let testStream: MediaStream | undefined
let testContext: AudioContext | undefined
let testFrame = 0
let testTimer = 0

const testRecordingUrl = ref('')
const testPlayer = ref<HTMLAudioElement | null>(null)
let testRecorder: MediaRecorder | undefined

const mutedPeers = ref<Record<string, boolean>>({})
const testPlaying = ref(false)
const testVolume = ref(100)

const routeReport = ref('')

type DiagnosticEvent = {
  at: string
  peer?: string
  event: string
  detail?: unknown
}

type RtpCounters = {
  bytes: number | null
  packets: number | null
  lost: number | null
  jitterMs: number | null
}

type LinkDiagnostic = {
  id: string
  name: string
  closed: boolean
  polling: boolean
  statsError: boolean
  candidates: Record<string, number>
  selectedPair?: string
  samples: Record<string, unknown>[]
}

const diagnosticsAvailable = ref(false)
const diagnosticsText = ref('')
const diagnosticsStatus = ref('')
const diagnosticEvents: DiagnosticEvent[] = []
const diagnosticLinks: LinkDiagnostic[] = []
let diagnosticSequence = 0
let diagnosticEventsDropped = 0
let diagnosticLinksDropped = 0
let diagnosticTimer = 0

function recordDiagnostic(event: string, link?: Link, detail?: unknown): void {
  diagnosticEvents.push({
    at: new Date().toISOString(),
    peer: link?.diagnostic.id,
    event,
    detail
  })
  if (diagnosticEvents.length > 512) {
    diagnosticEvents.shift()
    diagnosticEventsDropped++
  }
  diagnosticsAvailable.value = true
}

function newLinkDiagnostic(peer: AudioPeer): LinkDiagnostic {
  const id = `peer-${++diagnosticSequence}`
  const diagnostic: LinkDiagnostic = {
    id,
    name: peer.displayName || id,
    closed: false,
    polling: false,
    statsError: false,
    candidates: {},
    samples: []
  }
  diagnosticLinks.push(diagnostic)
  const closed = diagnosticLinks.filter((item) => item.closed)
  if (closed.length > 8) {
    diagnosticLinks.splice(diagnosticLinks.indexOf(closed[0]), 1)
    diagnosticLinksDropped++
  }
  return diagnostic
}

function diagnosticNumber(value: unknown, scale = 1): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value * scale : null
}

function readRtp(row: Record<string, unknown>, incoming: boolean): RtpCounters {
  return {
    bytes: diagnosticNumber(incoming ? row.bytesReceived : row.bytesSent),
    packets: diagnosticNumber(incoming ? row.packetsReceived : row.packetsSent),
    lost: incoming ? diagnosticNumber(row.packetsLost) : null,
    jitterMs: incoming ? diagnosticNumber(row.jitter, 1000) : null
  }
}

function trackDiagnostic(track: MediaStreamTrack | null | undefined): object | null {
  if (!track) return null
  return { state: track.readyState, enabled: track.enabled, muted: track.muted }
}

async function sampleLink(link: Link): Promise<void> {
  const { pc, diagnostic } = link
  if (!current(link) || diagnostic.polling) return
  diagnostic.polling = true

  try {
    const stats = await pc.getStats()
    if (!current(link)) return
    diagnostic.statsError = false

    const rows = [...stats.values()]
    const transport = rows.find((row) => row.type === 'transport' && row.selectedCandidatePairId)
    const pairId = transport?.selectedCandidatePairId as string | undefined
    const pair = pairId ? stats.get(pairId) : undefined
    const local = pair ? stats.get(pair.localCandidateId) : undefined
    const remote = pair ? stats.get(pair.remoteCandidateId) : undefined
    const route = pair
      ? {
          local: local?.candidateType ?? null,
          remote: remote?.candidateType ?? null,
          protocol: local?.protocol ?? null,
          relayProtocol: local?.relayProtocol ?? null
        }
      : null

    if (diagnostic.selectedPair !== pairId) {
      diagnostic.selectedPair = pairId
      recordDiagnostic('ice.selected-pair-changed', link, route)
    }

    const audio = rows.filter(
      (row) =>
        (row.kind ?? row.mediaType) === 'audio' &&
        (row.type === 'inbound-rtp' || row.type === 'outbound-rtp')
    )

    diagnostic.samples.push({
      at: new Date().toISOString(),
      ice: pc.iceConnectionState,
      connection: pc.connectionState,
      dtls: transport?.dtlsState ?? null,
      route,
      rttMs: diagnosticNumber(pair?.currentRoundTripTime, 1000),
      microphoneOn: microphone.value,
      sendingTrack: trackDiagnostic(link.sender?.track),
      receivingTrack: trackDiagnostic(link.remote?.getAudioTracks()[0]),
      player: link.player
        ? { muted: link.player.muted, paused: link.player.paused, volume: link.player.volume }
        : null,
      audio: audio.map((row) => ({
        stream: row.id,
        direction: row.type === 'inbound-rtp' ? 'in' : 'out',
        ...readRtp(row, row.type === 'inbound-rtp')
      }))
    })
    if (diagnostic.samples.length > 60) diagnostic.samples.shift()
  } catch {
    if (current(link) && !diagnostic.statsError) {
      diagnostic.statsError = true
      recordDiagnostic('stats.read-failed', link)
    }
  } finally {
    diagnostic.polling = false
  }
}

function resetDiagnostics(): void {
  diagnosticEvents.length = 0
  diagnosticLinks.length = 0
  diagnosticSequence = 0
  diagnosticEventsDropped = 0
  diagnosticLinksDropped = 0
  diagnosticsAvailable.value = false
  diagnosticsText.value = ''
  diagnosticsStatus.value = ''
}

async function copyDiagnostics(): Promise<void> {
  const text = JSON.stringify(
    {
      format: 'zvonilka-media-diagnostics-v1',
      exportedAt: new Date().toISOString(),
      userAgent: navigator.userAgent,
      diagnosticEventsDropped,
      diagnosticLinksDropped,
      events: diagnosticEvents,
      links: diagnosticLinks.map((item) => ({
        id: item.id,
        name: item.name,
        closed: item.closed,
        gatheredCandidates: item.candidates,
        samples: item.samples
      }))
    },
    null,
    2
  )

  diagnosticsText.value = ''
  diagnosticsStatus.value = ''
  try {
    await navigator.clipboard.writeText(text)
    diagnosticsStatus.value = 'Диагностика скопирована.'
  } catch {
    diagnosticsText.value = text
    diagnosticsStatus.value = 'Буфер обмена недоступен. Скопируйте отчёт из поля ниже.'
  }
}

async function describeRoute(link: Link): Promise<string> {
  const { pc } = link
  const stats = await pc.getStats()

  for (const report of stats.values()) {
    if (report.type !== 'transport' || !report.selectedCandidatePairId) continue

    const pair = stats.get(report.selectedCandidatePairId)
    const local = pair ? stats.get(pair.localCandidateId) : undefined
    const remote = pair ? stats.get(pair.remoteCandidateId) : undefined
    if (!local?.candidateType || !remote?.candidateType) continue

    const viaTurn = local.candidateType === 'relay' || remote.candidateType === 'relay'
    const route = viaTurn ? 'через TURN' : 'прямой'
    const protocol = local.protocol ? ` · ${local.protocol}` : ''
    const relay = local.relayProtocol ? ` · до TURN: ${local.relayProtocol}` : ''

    return `${route} · ${local.candidateType} → ${remote.candidateType}${protocol}${relay}`
  }

  return `маршрут пока не определён (ICE: ${pc.iceConnectionState})`
}

let routeRequest = 0

async function refreshRouteReport(): Promise<void> {
  const request = ++routeRequest
  routeReport.value = ''

  const rows = await Promise.all(
    peers.value.map(async (peer, index) => {
      const label = peerLabel(peer, index)
      const link = links.get(key(peer))
      if (!link) return `${label}: активного аудиосоединения нет`

      try {
        const route = await describeRoute(link)
        return current(link) ? `${label}: ${route}` : `${label}: соединение изменилось`
      } catch {
        return `${label}: не удалось прочитать статистику`
      }
    })
  )

  if (request === routeRequest) routeReport.value = rows.join('\n')
}

function togglePeerMute(peer: AudioPeer): void {
  const id = key(peer)
  const player = links.get(id)?.player
  if (!player) return

  player.muted = !player.muted
  mutedPeers.value[id] = player.muted

  const link = links.get(id)
  if (link) recordDiagnostic('player.mute-changed', link, { muted: player.muted })

  if (!player.muted && player.paused && player.srcObject) {
    void player.play().catch(() => {})
  }
}

function peerLabel(peer: AudioPeer, index: number): string {
  return peer.displayName || `Собеседник ${index + 1}`
}

function setPlayerVolume(player: HTMLAudioElement | null | undefined, event: Event): void {
  const input = event.currentTarget
  if (!player || !(input instanceof HTMLInputElement)) return
  player.volume = input.valueAsNumber / 100
}

function setTestVolume(event: Event): void {
  const input = event.currentTarget
  if (!(input instanceof HTMLInputElement)) return
  testVolume.value = input.valueAsNumber
  setPlayerVolume(testPlayer.value, event)
}

function toggleTestPlayback(): void {
  const player = testPlayer.value
  if (!player) return

  if (player.paused) {
    void player.play().catch(() => {
      microphoneError.value = 'Не удалось воспроизвести тестовую запись.'
    })
  } else {
    player.pause()
  }
}

function restartTestPlayback(): void {
  const player = testPlayer.value
  if (!player) return

  player.currentTime = 0
  if (player.paused) toggleTestPlayback()
}

function clearTestRecording(): void {
  testPlaying.value = false
  testPlayer.value?.pause()
  if (testRecordingUrl.value) URL.revokeObjectURL(testRecordingUrl.value)
  testRecordingUrl.value = ''
}

function startTestRecording(input: MediaStream, attempt: number): void {
  const recorder = new MediaRecorder(input)
  const chunks: Blob[] = []
  testRecorder = recorder

  recorder.ondataavailable = ({ data }) => {
    if (data.size > 0) chunks.push(data)
  }

  recorder.onstop = () => {
    if (testRecorder !== recorder || attempt !== ticket) return
    testRecorder = undefined
    stopMicrophoneTest()
    busy.value = false

    const recording = new Blob(chunks, { type: recorder.mimeType })
    if (!recording.size) {
      microphoneError.value = 'Запись пуста. Запустите проверку ещё раз.'
      return
    }

    const url = URL.createObjectURL(recording)
    testRecordingUrl.value = url
    void nextTick(() => {
      if (testRecordingUrl.value !== url) return
      if (testPlayer.value) testPlayer.value.volume = testVolume.value / 100
      void testPlayer.value?.play().catch(() => {
        // Если автовоспроизведение заблокировано, доступны controls.
      })
    })
  }

  recorder.onerror = () => {
    if (testRecorder !== recorder || attempt !== ticket) return
    stopMicrophoneTest()
    busy.value = false
    microphoneError.value = 'Не удалось записать звук для проверки.'
  }

  recorder.start()
}

function finishMicrophoneTest(): void {
  const recorder = testRecorder
  if (!recorder || recorder.state === 'inactive') return
  busy.value = true
  window.clearTimeout(testTimer)
  recorder.stop()
}

async function refreshMicrophones(): Promise<void> {
  const request = ++deviceRequest
  try {
    const devices = await navigator.mediaDevices.enumerateDevices()
    if (disposed || request !== deviceRequest) return

    microphones.value = devices.filter(
      (device) =>
        device.kind === 'audioinput' &&
        Boolean(device.deviceId) &&
        device.deviceId !== 'default' &&
        device.deviceId !== 'communications'
    )
  } catch {
    // Не сбрасываем выбранный вход при временной ошибке перечисления.
  }
}

function openMicrophone(): Promise<MediaStream> {
  const deviceId = selectedMicrophone.value
  return navigator.mediaDevices.getUserMedia({
    audio: deviceId ? { deviceId: { exact: deviceId } } : true
  })
}

function microphoneErrorText(error: unknown): string {
  if (error instanceof DOMException) {
    if (error.name === 'NotAllowedError') {
      return 'Нет доступа к микрофону. Проверьте разрешения системы.'
    }
    if (error.name === 'NotFoundError' || error.name === 'OverconstrainedError') {
      return 'Выбранный микрофон недоступен. Выберите другой вход.'
    }
    if (error.name === 'NotReadableError') {
      return 'Не удалось открыть микрофон. Проверьте устройство и другие приложения.'
    }
  }
  return 'Не удалось запустить микрофон.'
}

function stopMicrophoneTest(): void {
  const recorder = testRecorder
  testRecorder = undefined
  if (recorder) {
    recorder.ondataavailable = null
    recorder.onstop = null
    recorder.onerror = null
    if (recorder.state !== 'inactive') recorder.stop()
  }

  window.cancelAnimationFrame(testFrame)
  window.clearTimeout(testTimer)
  testStream?.getTracks().forEach((track) => track.stop())
  testStream = undefined

  const context = testContext
  testContext = undefined
  if (context) void context.close().catch(() => {})

  testing.value = false
  level.value = 0
}

async function toggleMicrophoneTest(): Promise<void> {
  if (busy.value || microphone.value || !peers.value.length) return
  if (testing.value) {
    // stopMicrophoneTest()
    finishMicrophoneTest()
    return
  }

  clearTestRecording()

  const attempt = ++ticket
  let input: MediaStream | undefined
  microphoneError.value = ''
  busy.value = true

  try {
    // Создаём контекст из обработчика нажатия кнопки.
    const context = new AudioContext()
    testContext = context

    input = await openMicrophone()
    if (attempt !== ticket) return
    testStream = input
    void refreshMicrophones()

    const source = context.createMediaStreamSource(input)
    const analyser = context.createAnalyser()
    analyser.fftSize = 1024
    source.connect(analyser)
    // Не подключаем анализатор к context.destination: звука в колонках нет.
    const samples = new Float32Array(analyser.fftSize)

    await context.resume()
    if (attempt !== ticket) return
    startTestRecording(input, attempt)
    testing.value = true

    function sample(): void {
      if (attempt !== ticket || !testing.value) return
      analyser.getFloatTimeDomainData(samples)
      let sum = 0
      for (const value of samples) sum += value * value
      const rms = Math.sqrt(sum / samples.length)
      level.value = Math.min(100, Math.round(rms * 400))
      testFrame = window.requestAnimationFrame(sample)
    }

    testTimer = window.setTimeout(finishMicrophoneTest, 10_000)
    sample()
  } catch (error) {
    if (attempt === ticket) {
      stopMicrophoneTest()
      microphoneError.value = microphoneErrorText(error)
    }
  } finally {
    if (input && input !== testStream) input.getTracks().forEach((track) => track.stop())
    if (attempt === ticket) busy.value = false
  }
}

onMounted(() => {
  diagnosticTimer = window.setInterval(() => {
    for (const link of links.values()) void sampleLink(link)
  }, 2000)
  navigator.mediaDevices.addEventListener('devicechange', refreshMicrophones)
  void refreshMicrophones()
})

function key(peer: AudioPeer): string {
  return `${peer.roomId}:${peer.userId}:${peer.generation}`
}

function current(link: Link): boolean {
  return links.get(key(link.peer)) === link
}

function setNote(link: Link, text: string): void {
  notes.value[key(link.peer)] = text
}

function drop(link: Link): void {
  link.diagnostic.closed = true
  recordDiagnostic('peer.closed', link, {
    connection: link.pc.connectionState,
    ice: link.pc.iceConnectionState
  })
  delete mutedPeers.value[key(link.peer)]
  links.delete(key(link.peer))
  link.cancelGather?.()
  link.pc.ontrack = null
  link.pc.onconnectionstatechange = null
  link.pc.close()
  if (link.player) link.player.srcObject = null
  delete notes.value[key(link.peer)]
}

function fail(link: Link): void {
  if (!current(link)) return
  drop(link)
  setNote(link, 'Нет связи с этим участником. Можно переподключиться к комнате.')
}

function stopCapture(): void {
  ticket++
  stopMicrophoneTest()
  clearTestRecording()
  capture?.getTracks().forEach((track) => track.stop())
  capture = undefined
  microphone.value = false
  busy.value = false
  microphoneError.value = ''
}

function play(link: Link): void {
  if (!link.player || !link.remote || link.player.srcObject === link.remote) return
  link.player.srcObject = link.remote
  void link.player.play().catch(() => {
    if (current(link)) setNote(link, 'Автозапуск не сработал. Нажмите кнопку звука дважды.')
  })
}

function bindPlayer(peer: AudioPeer, el: Element | ComponentPublicInstance | null): void {
  const link = links.get(key(peer))
  if (!link) return
  link.player = el instanceof HTMLAudioElement ? el : undefined
  play(link)
}

function createLink(peer: AudioPeer): Link {
  const pc = new RTCPeerConnection({ iceServers: [] })
  const link: Link = { peer, pc, diagnostic: newLinkDiagnostic(peer) }
  links.set(key(peer), link)

  recordDiagnostic('peer.created', link, {
    initiator: peer.initiator,
    ice: pc.iceConnectionState,
    connection: pc.connectionState
  })

  pc.addEventListener('icecandidate', ({ candidate }) => {
    if (!current(link) || !candidate?.type) return
    const counts = link.diagnostic.candidates
    counts[candidate.type] = (counts[candidate.type] ?? 0) + 1
  })

  pc.addEventListener('icecandidateerror', ({ errorCode, url }) => {
    if (!current(link)) return
    recordDiagnostic('ice.candidate-error', link, {
      code: errorCode,
      serverKind: /^(stun[s]?|turn[s]?):/i.exec(url)?.[1]?.toLowerCase() ?? null
    })
  })

  pc.addEventListener('icegatheringstatechange', () => {
    if (!current(link)) return
    recordDiagnostic('ice.gathering-state', link, {
      state: pc.iceGatheringState,
      candidates: { ...link.diagnostic.candidates }
    })
  })

  pc.addEventListener('iceconnectionstatechange', () => {
    if (!current(link)) return
    recordDiagnostic('ice.connection-state', link, pc.iceConnectionState)
  })

  setNote(link, 'Подключение…')
  pc.ontrack = ({ track }) => {
    if (!current(link)) return
    link.remote = new MediaStream([track])
    play(link)
  }
  pc.onconnectionstatechange = () => {
    if (!current(link)) return
    recordDiagnostic('peer.connection-state', link, pc.connectionState)
    if (pc.connectionState === 'connected') {
      if (notes.value[key(peer)] === 'Подключение…') setNote(link, 'Аудиосвязь установлена.')
    } else if (pc.connectionState === 'failed') {
      fail(link)
    }
  }
  return link
}

function gather(link: Link): Promise<void> {
  const { pc } = link
  if (pc.iceGatheringState === 'complete') return Promise.resolve()
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(cancel, 5000)
    function cleanup(): void {
      window.clearTimeout(timer)
      pc.removeEventListener('icegatheringstatechange', check)
      link.cancelGather = undefined
    }
    function cancel(): void {
      cleanup()
      reject(new Error('ICE gathering interrupted'))
    }
    function check(): void {
      if (pc.iceGatheringState !== 'complete') return
      cleanup()
      resolve()
    }
    link.cancelGather = cancel
    pc.addEventListener('icegatheringstatechange', check)
    check()
  })
}

async function sendDescription(link: Link, type: AudioSignal['type']): Promise<void> {
  if (!current(link)) return
  await gather(link)
  if (!current(link)) return
  const sdp = link.pc.localDescription?.sdp
  if (!sdp || !(await window.api.sendAudio(link.peer, { type, sdp }))) fail(link)
}

async function attach(link: Link, transceiver: RTCRtpTransceiver): Promise<void> {
  transceiver.direction = 'sendrecv'
  link.sender = transceiver.sender
  const track = capture?.getAudioTracks()[0]
  if (track) await link.sender.replaceTrack(track)
}

async function offer(link: Link): Promise<void> {
  const { pc } = link
  await attach(link, pc.addTransceiver('audio', { direction: 'sendrecv' }))
  if (!current(link)) return
  await pc.setLocalDescription(await pc.createOffer())
  await sendDescription(link, 'offer')
}

async function receive(link: Link, signal: AudioSignal): Promise<void> {
  const { pc } = link
  if (signal.type === 'answer') {
    if (pc.signalingState === 'have-local-offer') await pc.setRemoteDescription(signal)
    return
  }
  if (link.sender || pc.signalingState !== 'stable') return
  await pc.setRemoteDescription(signal)
  if (!current(link)) return
  const transceiver = pc.getTransceivers().find((item) => item.receiver.track.kind === 'audio')
  if (!transceiver) throw new Error('Audio transceiver missing')
  await attach(link, transceiver)
  if (!current(link)) return
  await pc.setLocalDescription(await pc.createAnswer())
  await sendDescription(link, 'answer')
}

function update(next: AudioPeer[]): void {
  if (!peers.value.length && next.length) {
    resetDiagnostics()
    recordDiagnostic('media.started', undefined, { peers: next.length })
  } else if (peers.value.length && !next.length) {
    recordDiagnostic('media.no-peers')
  }
  routeRequest++
  routeReport.value = ''
  const keys = new Set(next.map(key))
  for (const link of links.values()) {
    if (!keys.has(key(link.peer))) drop(link)
  }
  for (const id of Object.keys(notes.value)) {
    if (!keys.has(id)) delete notes.value[id]
  }
  const previous = new Set(peers.value.map(key))
  peers.value = next
  if (next.length === 0) stopCapture()
  for (const peer of next) {
    if (previous.has(key(peer))) continue
    try {
      const link = createLink(peer)
      if (peer.initiator) void offer(link).catch(() => fail(link))
    } catch {
      notes.value[key(peer)] = 'Не удалось создать аудиосоединение.'
    }
  }
}

const unsubscribe = window.api.onAudio((event) => {
  if ('peers' in event) {
    update(event.peers)
  } else if ('signal' in event) {
    const link = links.get(key(event.peer))
    if (link) void receive(link, event.signal).catch(() => fail(link))
  }
})

async function toggleMicrophone(): Promise<void> {
  if (busy.value || testing.value || !peers.value.length) return

  testPlayer.value?.pause()

  microphoneError.value = ''
  busy.value = true
  const attempt = ++ticket
  let input: MediaStream | undefined
  try {
    if (capture) {
      capture.getTracks().forEach((track) => track.stop())
      capture = undefined
      microphone.value = false
    } else {
      input = await openMicrophone()
      if (attempt !== ticket) return
      if (!input.getAudioTracks().length) throw new Error('Microphone track missing')
      capture = input
      microphone.value = true
      void refreshMicrophones()
    }
    recordDiagnostic('microphone.changed', undefined, { enabled: microphone.value })
    const track = capture?.getAudioTracks()[0] ?? null
    await Promise.all(
      [...links.values()].map(async (link) => {
        try {
          await link.sender?.replaceTrack(track)
        } catch {
          if (current(link)) setNote(link, 'Не удалось переключить микрофон для этого участника.')
          recordDiagnostic('microphone.replace-track-failed', link)
        }
      })
    )
  } catch (error) {
    if (attempt === ticket) microphoneError.value = microphoneErrorText(error)
  } finally {
    if (input && input !== capture) input.getTracks().forEach((track) => track.stop())
    if (attempt === ticket) busy.value = false
  }
}

onBeforeUnmount(() => {
  window.clearInterval(diagnosticTimer)
  disposed = true
  navigator.mediaDevices.removeEventListener('devicechange', refreshMicrophones)
  unsubscribe()
  update([])
})
</script>

<template>
  <div v-if="peers.length" class="audio-links">
    <section class="audio-section" aria-label="Ваш микрофон">
      <div class="audio-heading">
        <h4>Ваш микрофон</h4>
        <span class="mic-state" :class="{ 'mic-state-live': microphone }" role="status">
          {{ testing ? 'Идёт проверка' : microphone ? 'Включён в звонке' : 'Выключен в звонке' }}
        </span>
      </div>

      <label class="device-field">
        <span>Устройство ввода</span>
        <select v-model="selectedMicrophone" :disabled="busy || testing || microphone">
          <option value="">Системный по умолчанию</option>
          <option
            v-if="
              selectedMicrophone &&
              !microphones.some((item) => item.deviceId === selectedMicrophone)
            "
            :value="selectedMicrophone"
            disabled
          >
            Выбранный микрофон недоступен
          </option>
          <option v-for="(item, index) in microphones" :key="item.deviceId" :value="item.deviceId">
            {{ item.label || `Микрофон ${index + 1}` }}
          </option>
        </select>
      </label>

      <div class="mic-actions">
        <button
          class="call-button"
          :class="{ 'call-button-live': microphone }"
          type="button"
          :disabled="busy || testing"
          @click="toggleMicrophone"
        >
          {{ microphone ? 'Выключить микрофон' : 'Включить микрофон' }}
        </button>
        <button type="button" :disabled="busy || microphone" @click="toggleMicrophoneTest">
          {{ testing ? 'Остановить и прослушать' : 'Проверить микрофон' }}
        </button>
      </div>

      <label v-if="testing" class="test-level">
        <span>Говорите в микрофон</span>
        <meter :value="level" min="0" max="100" />
      </label>

      <div v-if="testRecordingUrl" class="audio-item">
        <p class="audio-caption">Тестовая запись · слышите только вы</p>
        <audio
          ref="testPlayer"
          :src="testRecordingUrl"
          hidden
          @play="testPlaying = true"
          @pause="testPlaying = false"
          @ended="testPlaying = false"
        />
        <div class="audio-controls">
          <button type="button" :disabled="microphone || busy" @click="toggleTestPlayback">
            {{ testPlaying ? 'Пауза' : 'Слушать' }}
          </button>
          <button type="button" :disabled="microphone || busy" @click="restartTestPlayback">
            С начала
          </button>
          <input
            type="range"
            min="0"
            max="100"
            :value="testVolume"
            aria-label="Громкость тестовой записи"
            @input="setTestVolume"
          />
        </div>
      </div>

      <p v-if="microphoneError" class="audio-error" role="alert">{{ microphoneError }}</p>
    </section>

    <section class="audio-section" aria-label="Собеседники">
      <div class="audio-heading">
        <h4>Собеседники</h4>
        <span class="peer-count">{{ peers.length }}</span>
      </div>

      <button type="button" @click="refreshRouteReport">Сетевой отчёт</button>
      <p v-if="routeReport" class="route-report" role="status">{{ routeReport }}</p>

      <div v-for="(peer, index) in peers" :key="key(peer)" class="audio-item">
        <div class="audio-heading">
          <p class="peer-name">{{ peerLabel(peer, index) }}</p>
          <p v-if="notes[key(peer)]" class="audio-caption" role="status">
            {{ notes[key(peer)] }}
          </p>
        </div>

        <audio :ref="(el) => bindPlayer(peer, el)" hidden />

        <div class="audio-controls">
          <button
            type="button"
            :aria-label="`Заглушить: ${peerLabel(peer, index)}`"
            :aria-pressed="!!mutedPeers[key(peer)]"
            @click="togglePeerMute(peer)"
          >
            {{ mutedPeers[key(peer)] ? 'Звук выкл.' : 'Звук вкл.' }}
          </button>
          <input
            type="range"
            min="0"
            max="100"
            value="100"
            :aria-label="`Громкость: ${peerLabel(peer, index)}`"
            @input="setPlayerVolume(links.get(key(peer))?.player, $event)"
          />
        </div>
      </div>
    </section>
  </div>

  <details v-if="diagnosticsAvailable" class="diagnostics-panel">
    <summary>Диагностика звонка</summary>
    <button type="button" @click="copyDiagnostics">Скопировать диагностику</button>
    <p v-if="diagnosticsStatus" role="status">{{ diagnosticsStatus }}</p>
    <textarea
      v-if="diagnosticsText"
      :value="diagnosticsText"
      aria-label="Диагностика звонка"
      rows="8"
      readonly
    />
  </details>
</template>

<style scoped>
.audio-links,
.audio-section,
.device-field,
.mic-actions,
.test-level,
.audio-item {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  min-width: 0;
}

.audio-links {
  gap: 0.75rem;
}

.audio-section {
  gap: 0.625rem;
  padding: 0.75rem;
  border: 1px solid var(--ev-c-gray-3);
  border-radius: 0.625rem;
  background: var(--color-background-soft);
}

.audio-heading {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 0.25rem 0.625rem;
}

.audio-heading h4 {
  font-size: 1rem;
  font-weight: 600;
}

.mic-state,
.peer-count {
  padding: 0.125rem 0.5rem;
  border-radius: 1rem;
  background: var(--color-background-mute);
  font-size: 0.8125rem;
}

.mic-state-live {
  background: #203d32;
  color: #b7f0d1;
}

.device-field,
.mic-actions,
.test-level,
.audio-item {
  gap: 0.375rem;
}

.device-field,
.test-level,
.audio-caption {
  color: #c4c8d0;
  font-size: 0.875rem;
}

.audio-links select,
.audio-links button {
  width: 100%;
  min-width: 0;
  padding: 0.5rem 0.625rem;
  border: 1px solid var(--ev-c-gray-1);
  border-radius: 0.5rem;
  background: var(--color-background-mute);
  color: var(--color-text);
  color-scheme: dark;
  font: inherit;
}

.audio-links button {
  cursor: pointer;
}

.audio-links .call-button {
  border-color: #5478ab;
  background: #263c5b;
}

.audio-links .call-button-live {
  border-color: #588872;
  background: #203d32;
}

.audio-links button:not(:disabled):hover {
  border-color: #9cc5ff;
}

.audio-links button:disabled,
.audio-links select:disabled {
  opacity: 0.65;
  cursor: default;
}

.audio-links button:focus-visible,
.audio-links select:focus-visible {
  outline: 2px solid #9cc5ff;
  outline-offset: 2px;
}

.test-level meter {
  display: block;
  width: 100%;
  min-width: 0;
  max-width: 100%;
}

.audio-item {
  padding: 0.5rem;
  border-radius: 0.5rem;
  background: var(--color-background-mute);
}

.audio-error {
  color: #ffb4ab;
  overflow-wrap: anywhere;
}

.audio-links {
  gap: 0.5rem;
}

.audio-section {
  padding: 0.5rem;
  gap: 0.5rem;
}

.mic-actions,
.audio-controls {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.375rem;
}

.mic-actions button {
  flex: 1 1 8rem;
  width: auto;
}

.audio-controls button {
  width: auto;
  min-height: 2rem;
  padding: 0.25rem 0.5rem;
  font-size: 0.8125rem;
}

.audio-controls input[type='range'] {
  flex: 1 1 5rem;
  min-width: 0;
  max-width: 100%;
  min-height: 2rem;
  accent-color: #9cc5ff;
}

.peer-name {
  min-width: 0;
  overflow-wrap: anywhere;
}

.route-report {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  user-select: text;
  font-size: 0.8125rem;
}

.diagnostics-panel {
  min-width: 0;
  font-size: 0.875rem;
}

.diagnostics-panel summary {
  cursor: pointer;
}

.diagnostics-panel textarea {
  width: 100%;
  min-width: 0;
  max-width: 100%;
  user-select: text;
}
</style>
