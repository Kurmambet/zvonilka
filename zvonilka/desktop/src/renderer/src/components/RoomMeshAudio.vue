<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import type { ComponentPublicInstance } from 'vue'
import type { AudioPeer, AudioSignal, WsDiagnostic } from '../../../shared/audio'
import { useCallDiagnostics } from '../composables/useCallDiagnostics'
import type { LinkDiagnostic } from '../composables/useCallDiagnostics'

const props = defineProps<{ displayName?: string; iceMode: IceMode }>()

type Link = {
  peer: AudioPeer
  pc: RTCPeerConnection
  iceReady: Promise<boolean>
  sender?: RTCRtpSender
  remote?: MediaStream
  player?: HTMLAudioElement
  cancelGather?: () => void
  diagnostic: LinkDiagnostic
}

const links = new Map<string, Link>()
const peers = shallowRef<AudioPeer[]>([])
const notes = ref<Record<string, string>>({})
const mutedPeers = ref<Record<string, boolean>>({})

const microphoneError = ref('')
const microphone = ref(false)
const busy = ref(false)
const microphones = shallowRef<MediaDeviceInfo[]>([])
const selectedMicrophone = ref('')
let capture: MediaStream | undefined
let ticket = 0
let disposed = false
let deviceRequest = 0

const testing = ref(false)
const level = ref(0)
const testRecordingUrl = ref('')
const testPlayer = ref<HTMLAudioElement | null>(null)
const testPlaying = ref(false)
const testVolume = ref(100)
let testStream: MediaStream | undefined
let testContext: AudioContext | undefined
let testRecorder: MediaRecorder | undefined
let testFrame = 0
let testTimer = 0

const diagnostics = useCallDiagnostics()
const diagnosticsAvailable = diagnostics.available
const diagnosticsText = ref('')
const diagnosticsStatus = ref('')
const routeReport = ref('')
type IceMode = 'all' | 'relay'

const lastIceMode = ref<IceMode | null>(null)

function iceModeLabel(mode: IceMode): string {
  return mode === 'relay' ? 'Только TURN' : 'Авто'
}
let diagnosticTimer = 0
let copyRequest = 0
let routeRequest = 0

const wsDiagnostics = shallowRef<WsDiagnostic[]>([])
let currentAuthRequestId: string | undefined

function recordWs(item: WsDiagnostic): void {
  if (item.event === 'ws.started') {
    diagnostics.reset()
    copyRequest++
    diagnosticsText.value = ''
    diagnosticsStatus.value = ''
    currentAuthRequestId = undefined
    wsDiagnostics.value = [item]
    return
  }

  // Поздний close от сокета, который заменили после обновления токена,
  // не должен выглядеть как закрытие новой попытки.
  if (item.event === 'ws.attempt') currentAuthRequestId = item.authRequestId
  if (item.authRequestId && item.authRequestId !== currentAuthRequestId) return

  const rows = wsDiagnostics.value
  if (item.event === 'ws.heartbeat-ack') {
    wsDiagnostics.value = [...rows.filter((row) => row.event !== 'ws.heartbeat-ack'), item].slice(
      -64
    )
  } else {
    wsDiagnostics.value = [...rows, item].slice(-64)
  }
}

function key(peer: AudioPeer): string {
  return `${peer.roomId}:${peer.userId}:${peer.generation}`
}

function current(link: Link): boolean {
  return links.get(key(link.peer)) === link
}

function peerLabel(peer: AudioPeer, index: number): string {
  return peer.displayName || `Собеседник ${index + 1}`
}

function setNote(link: Link, text: string): void {
  notes.value[key(link.peer)] = text
}

async function copyDiagnostics(kind: 'summary' | 'json'): Promise<void> {
  const attempt = ++copyRequest
  const media = diagnostics.format(props.displayName ?? '', kind)
  const rows = wsDiagnostics.value

  const text =
    kind === 'json'
      ? JSON.stringify({
          ...JSON.parse(media),
          iceTransportPolicy: lastIceMode.value,
          websocket: rows
        })
      : [
          `Режим ICE: ${lastIceMode.value ? iceModeLabel(lastIceMode.value) : 'нет данных'}`,
          media,
          '',
          'WebSocket:',
          ...(rows.length
            ? rows.map((row) =>
                [
                  row.at,
                  row.event,
                  row.authRequestId ? `auth=${row.authRequestId}` : '',
                  row.generation ? `generation=${row.generation}` : '',
                  row.reason ? `reason=${row.reason}` : '',
                  row.closeCode !== undefined ? `close=${row.closeCode}` : '',
                  row.wasClean !== undefined ? `clean=${row.wasClean}` : ''
                ]
                  .filter(Boolean)
                  .join(' · ')
              )
            : ['нет данных'])
        ].join('\n')
  diagnosticsText.value = ''
  diagnosticsStatus.value = ''

  try {
    await navigator.clipboard.writeText(text)
    if (attempt === copyRequest) diagnosticsStatus.value = 'Диагностика скопирована.'
  } catch {
    if (attempt !== copyRequest) return
    diagnosticsText.value = text
    diagnosticsStatus.value = 'Буфер обмена недоступен. Скопируйте текст из поля ниже.'
  }
}

async function refreshRouteReport(): Promise<void> {
  const request = ++routeRequest
  routeReport.value = ''

  const rows = await Promise.all(
    peers.value.map(async (peer, index) => {
      const label = peerLabel(peer, index)
      const link = links.get(key(peer))
      if (!link) return `${label}: активного аудиосоединения нет`

      try {
        const route = await diagnostics.routeNow(link.pc)
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
  const link = links.get(id)
  const player = link?.player
  if (!link || !player) return

  player.muted = !player.muted
  mutedPeers.value[id] = player.muted
  diagnostics.record('player.mute-changed', link, { muted: player.muted })

  if (!player.muted && player.paused && player.srcObject) {
    void player.play().catch(() => {})
  }
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
      const player = testPlayer.value
      if (!player || testRecordingUrl.value !== url || microphone.value) return
      player.volume = testVolume.value / 100
      void player.play().catch(() => {})
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
    // Сохраняем выбранное устройство при временной ошибке перечисления.
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
    finishMicrophoneTest()
    return
  }

  clearTestRecording()
  const attempt = ++ticket
  let input: MediaStream | undefined
  microphoneError.value = ''
  busy.value = true

  try {
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
      level.value = Math.min(100, Math.round(Math.sqrt(sum / samples.length) * 400))
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

function drop(link: Link): void {
  diagnostics.closeLink(link)
  const id = key(link.peer)
  delete mutedPeers.value[id]
  links.delete(id)
  link.cancelGather?.()
  link.pc.ontrack = null
  link.pc.onconnectionstatechange = null
  link.pc.close()
  if (link.player) link.player.srcObject = null
  delete notes.value[id]
}

function fail(link: Link): void {
  if (!current(link)) return
  drop(link)
  setNote(link, 'Нет связи с этим участником. Можно переподключиться к комнате.')
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
  const policy = props.iceMode
  lastIceMode.value = policy
  const pc = new RTCPeerConnection({ iceTransportPolicy: policy })
  const iceReady = window.api
    .getIceServers()
    .then((result) => {
      if (pc.signalingState === 'closed') return false
      if (!result.ok) return policy === 'all'
      if (
        policy === 'relay' &&
        !result.value.some((item) => item.urls.some((url) => /^turns?:/.test(url)))
      ) {
        return false
      }
      pc.setConfiguration({ iceServers: result.value, iceTransportPolicy: policy })
      return true
    })
    .catch(() => policy === 'all')
  const link: Link = { peer, pc, iceReady, diagnostic: diagnostics.createLink(peer.displayName) }
  links.set(key(peer), link)

  diagnostics.record('peer.created', link, {
    initiator: peer.initiator,
    ice: pc.iceConnectionState,
    connection: pc.connectionState
  })
  diagnostics.observeIce(link, () => current(link))

  setNote(link, 'Подключение…')
  pc.ontrack = ({ track }) => {
    if (!current(link)) return
    link.remote = new MediaStream([track])
    play(link)
  }
  pc.onconnectionstatechange = () => {
    if (!current(link)) return
    diagnostics.record('peer.connection-state', link, pc.connectionState)
    if (pc.connectionState === 'connected') {
      if (notes.value[key(peer)] === 'Подключение…') setNote(link, 'Аудиосвязь установлена.')
    } else if (pc.connectionState === 'failed') {
      fail(link)
    }
  }

  return link
}

function gather(link: Link): Promise<'complete' | 'timeout'> {
  const { pc } = link
  if (pc.iceGatheringState === 'complete') return Promise.resolve('complete')

  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => {
      cleanup()
      resolve('timeout')
    }, 5000)

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
      resolve('complete')
    }

    link.cancelGather = cancel
    pc.addEventListener('icegatheringstatechange', check)
    check()
  })
}

async function sendDescription(link: Link, type: AudioSignal['type']): Promise<void> {
  if (!current(link)) return
  const gathering = await gather(link)
  if (!current(link)) return
  const sdp = link.pc.localDescription?.sdp
  const relayOnly = link.pc.getConfiguration().iceTransportPolicy === 'relay'
  const candidates = sdp?.split(/\r?\n/).filter((line) => line.startsWith('a=candidate:')) ?? []
  const usable = relayOnly
    ? candidates.some((line) => /\s+typ\s+relay(?:\s|$)/i.test(line))
    : candidates.length > 0
  if (gathering === 'timeout') {
    diagnostics.record('ice.gather-timeout', link, { type, relayOnly, usable })
  }
  if (!sdp || !usable) {
    drop(link)
    setNote(link, relayOnly ? 'Не найден кандидат TURN.' : 'Не найден кандидат ICE.')
    return
  }
  const sent = await window.api.sendAudio(link.peer, { type, sdp })
  if (!current(link)) return
  if (sent) diagnostics.record('sdp.sent', link, { type, gathering })
  else fail(link)
}

async function attach(link: Link, transceiver: RTCRtpTransceiver): Promise<void> {
  transceiver.direction = 'sendrecv'
  link.sender = transceiver.sender
  const track = capture?.getAudioTracks()[0]
  if (track) await link.sender.replaceTrack(track)
}

async function waitForIce(link: Link): Promise<boolean> {
  const ready = await link.iceReady
  if (!current(link)) return false
  if (ready) return true
  drop(link)
  setNote(link, 'Режим «Только TURN»: сервер не выдал настройки реле.')
  return false
}

async function offer(link: Link): Promise<void> {
  if (!(await waitForIce(link))) return
  const { pc } = link
  await attach(link, pc.addTransceiver('audio', { direction: 'sendrecv' }))
  if (!current(link)) return
  await pc.setLocalDescription(await pc.createOffer())
  await sendDescription(link, 'offer')
}

async function receive(link: Link, signal: AudioSignal): Promise<void> {
  if (!(await waitForIce(link))) return
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
    diagnostics.reset()
    copyRequest++
    diagnosticsStatus.value = ''
    diagnosticsText.value = ''
    diagnostics.record('media.started', undefined, { peers: next.length })
  } else if (peers.value.length && !next.length) {
    diagnostics.record('media.no-peers')
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
  if ('wsDiagnostic' in event) {
    recordWs(event.wsDiagnostic)
  } else if ('peers' in event) {
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

    diagnostics.record('microphone.changed', undefined, { enabled: microphone.value })
    const track = capture?.getAudioTracks()[0] ?? null

    await Promise.all(
      [...links.values()].map(async (link) => {
        try {
          await link.sender?.replaceTrack(track)
        } catch {
          if (!current(link)) return
          setNote(link, 'Не удалось переключить микрофон для этого участника.')
          diagnostics.record('microphone.replace-track-failed', link)
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

onMounted(() => {
  diagnosticTimer = window.setInterval(() => {
    for (const link of links.values()) {
      void diagnostics.sample(
        link,
        () => current(link),
        () => microphone.value
      )
    }
  }, 2000)

  navigator.mediaDevices.addEventListener('devicechange', refreshMicrophones)
  void refreshMicrophones()
})

onBeforeUnmount(() => {
  window.clearInterval(diagnosticTimer)
  copyRequest++
  disposed = true
  navigator.mediaDevices.removeEventListener('devicechange', refreshMicrophones)
  unsubscribe()
  update([])
})
</script>

<template>
  <div v-if="peers.length" class="audio-links">
    <section class="audio-section" aria-label="Собеседники">
      <div class="audio-heading">
        <h4>Собеседники</h4>
        <span class="peer-count">{{ peers.length }}</span>
      </div>

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

  <section v-if="peers.length" class="audio-section" aria-label="Ваш микрофон">
    <div class="audio-heading">
      <h4>Ваш микрофон</h4>
      <span class="mic-state" :class="{ 'mic-state-live': microphone }" role="status">
        {{ testing ? 'Идёт проверка' : microphone ? 'Включён в звонке' : 'Выключен в звонке' }}
      </span>
    </div>

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
      <slot />
    </div>
    <details class="sound-settings">
      <summary>Настройки звука</summary>
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
      <button type="button" :disabled="busy || microphone" @click="toggleMicrophoneTest">
        {{ testing ? 'Остановить и прослушать' : 'Проверить микрофон' }}
      </button>

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
    </details>

    <p v-if="microphoneError" class="audio-error" role="alert">{{ microphoneError }}</p>
  </section>
  <div v-else class="mic-actions"><slot /></div>

  <details v-if="diagnosticsAvailable || wsDiagnostics.length" class="diagnostics-panel">
    <summary>Диагностика звонка</summary>
    <button type="button" @click="refreshRouteReport">Сетевой отчёт</button>
    <p v-if="routeReport" class="route-report" role="status">{{ routeReport }}</p>
    <div class="audio-controls">
      <button type="button" @click="copyDiagnostics('summary')">Скопировать сводку</button>
      <button type="button" @click="copyDiagnostics('json')">Скопировать JSON</button>
    </div>
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
.test-level,
.audio-item {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  min-width: 0;
}

.audio-links,
.audio-section {
  gap: 0.5rem;
}

.audio-section {
  padding: 0.5rem;
  border: 1px solid var(--ev-c-gray-3);
  border-radius: 0.625rem;
  background: var(--color-background-soft);
}

.audio-heading,
.mic-actions,
.audio-controls {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.375rem;
}

.audio-heading {
  justify-content: space-between;
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

select,
button {
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

button {
  cursor: pointer;
}

button:not(:disabled):hover {
  border-color: #9cc5ff;
}

button:disabled,
select:disabled {
  opacity: 0.65;
  cursor: default;
}

button:focus-visible,
select:focus-visible {
  outline: 1px solid #9baec2;
  outline-offset: 2px;
}

.call-button {
  border-color: #5478ab;
  background: #263c5b;
}

.call-button-live {
  border-color: #588872;
  background: #203d32;
}

.test-level meter {
  display: block;
  width: 100%;
  min-width: 0;
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

.peer-name,
.route-report {
  overflow-wrap: anywhere;
  user-select: text;
}

.route-report {
  white-space: pre-wrap;
  font-size: 0.8125rem;
}

.diagnostics-panel {
  min-width: 0;
  font-size: 0.875rem;
}

.diagnostics-panel summary {
  cursor: pointer;
}

.diagnostics-panel .audio-controls {
  margin-block: 0.375rem;
}

.diagnostics-panel textarea {
  width: 100%;
  max-width: 100%;
  user-select: text;
}

.sound-settings summary {
  cursor: pointer;
}
.mic-actions :slotted(button) {
  flex: 1 1 8rem;
  width: auto;
}
.sound-settings > :not(summary) {
  margin-top: 0.5rem;
}
</style>
