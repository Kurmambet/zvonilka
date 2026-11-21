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

function clearTestRecording(): void {
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
    if (current(link)) setNote(link, 'Нажмите Play в плеере.')
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
  const link: Link = { peer, pc }
  links.set(key(peer), link)
  setNote(link, 'Подключение…')
  pc.ontrack = ({ track }) => {
    if (!current(link)) return
    link.remote = new MediaStream([track])
    play(link)
  }
  pc.onconnectionstatechange = () => {
    if (!current(link)) return
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
    const track = capture?.getAudioTracks()[0] ?? null
    await Promise.all(
      [...links.values()].map(async (link) => {
        try {
          await link.sender?.replaceTrack(track)
        } catch {
          if (current(link)) setNote(link, 'Не удалось переключить микрофон для этого участника.')
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
  disposed = true
  navigator.mediaDevices.removeEventListener('devicechange', refreshMicrophones)
  unsubscribe()
  update([])
})
</script>

<template>
  <div v-if="peers.length" class="audio-links">
    <label>
      Микрофон
      <select v-model="selectedMicrophone" :disabled="busy || testing || microphone">
        <option value="">Системный по умолчанию</option>
        <option
          v-if="
            selectedMicrophone && !microphones.some((item) => item.deviceId === selectedMicrophone)
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
    <label v-if="testing">
      Говорите в микрофон:
      <meter :value="level" min="0" max="100" />
    </label>
    <div v-if="testRecordingUrl">
      <p>Тестовая запись — слышите только вы</p>
      <audio
        ref="testPlayer"
        :src="testRecordingUrl"
        controls
        aria-label="Тестовая запись микрофона"
      />
    </div>
    <button type="button" :disabled="busy || testing" @click="toggleMicrophone">
      {{ microphone ? 'Выключить микрофон' : 'Включить микрофон' }}
    </button>
    <p v-if="microphoneError" role="alert">{{ microphoneError }}</p>
    <div v-for="peer in peers" :key="key(peer)">
      <p class="peer-id">{{ peer.userId }}</p>
      <p role="status">{{ notes[key(peer)] }}</p>
      <audio :ref="(el) => bindPlayer(peer, el)" controls aria-label="Звук собеседника" />
    </div>
  </div>
</template>

<style scoped>
.audio-links {
  display: grid;
  gap: 0.625rem;
}

.peer-id {
  overflow-wrap: anywhere;
  user-select: text;
}

audio {
  display: block;
  width: 100%;
}
</style>
