<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import type { AudioPeer } from '../../../shared/audio'

const player = ref<HTMLAudioElement | null>(null)
const available = ref(false)
const microphone = ref(false)
const requesting = ref(false)
const status = ref('')
let peer: AudioPeer | null = null
let pc: RTCPeerConnection | null = null
let sender: RTCRtpSender | null = null
let stream: MediaStream | null = null
let ticket = 0

function reset(): void {
  ticket++
  pc?.close()
  pc = null
  sender = null
  stream?.getTracks().forEach((track) => track.stop())
  stream = null
  microphone.value = false
  requesting.value = false
  if (player.value) player.value.srcObject = null
}

function isCurrentConnection(connection: RTCPeerConnection): boolean {
  return pc === connection
}

function createConnection(): RTCPeerConnection {
  const connection = new RTCPeerConnection({ iceServers: [] })
  pc = connection
  connection.ontrack = ({ track }) => {
    const output = player.value
    if (pc !== connection || !output) return
    output.srcObject = new MediaStream([track])
    void output.play().catch(() => {
      if (pc === connection) status.value = 'Нажмите Play в аудиоплеере.'
    })
  }
  connection.onconnectionstatechange = () => {
    if (pc !== connection) return
    if (connection.connectionState === 'connected') status.value = 'Аудиосвязь установлена.'
    if (connection.connectionState === 'failed') fail(connection)
  }
  return connection
}

function fail(connection: RTCPeerConnection): void {
  if (pc !== connection) return
  reset()
  available.value = false
  status.value = 'Аудиосвязь не установлена. Отключитесь и подключитесь к комнате снова.'
}

function gather(connection: RTCPeerConnection): Promise<void> {
  return new Promise((resolve, reject) => {
    if (connection.iceGatheringState === 'complete') {
      resolve()
      return
    }
    const timer = window.setTimeout(() => {
      connection.removeEventListener('icegatheringstatechange', check)
      reject(new Error('ICE timeout'))
    }, 5000)
    function check(): void {
      if (connection.iceGatheringState !== 'complete') return
      window.clearTimeout(timer)
      connection.removeEventListener('icegatheringstatechange', check)
      resolve()
    }
    connection.addEventListener('icegatheringstatechange', check)
    check()
  })
}

async function sendDescription(
  connection: RTCPeerConnection,
  target: AudioPeer,
  type: 'offer' | 'answer'
): Promise<void> {
  await gather(connection)
  if (pc !== connection) return
  const sdp = connection.localDescription?.sdp
  if (!sdp || !(await window.api.sendAudio(target, { type, sdp }))) fail(connection)
}

const unsubscribe = window.api.onAudio((event) => {
  if (!('signal' in event)) {
    reset()
    peer = event.peer
    available.value = peer !== null
    status.value = peer ? 'Устанавливаем аудиосвязь…' : ''
    if (!peer?.initiator) return
    const target = peer
    const connection = createConnection()
    sender = connection.addTransceiver('audio', { direction: 'sendrecv' }).sender
    void (async () => {
      await connection.setLocalDescription(await connection.createOffer())
      await sendDescription(connection, target, 'offer')
    })().catch(() => fail(connection))
    return
  }

  const target = peer
  if (!target || target.generation !== event.peer.generation) return
  const signal = event.signal
  if (signal.type === 'answer') {
    const connection = pc
    if (connection?.signalingState === 'have-local-offer') {
      void connection.setRemoteDescription(signal).catch(() => fail(connection))
    }
    return
  }

  if (pc || target.initiator) return
  const connection = createConnection()
  void (async () => {
    await connection.setRemoteDescription(signal)
    if (!isCurrentConnection(connection)) return
    const transceiver = connection
      .getTransceivers()
      .find((item) => item.receiver.track.kind === 'audio')
    if (!transceiver) throw new Error('No audio transceiver')
    transceiver.direction = 'sendrecv'
    sender = transceiver.sender
    await connection.setLocalDescription(await connection.createAnswer())
    await sendDescription(connection, target, 'answer')
  })().catch(() => fail(connection))
})

async function toggleMicrophone(): Promise<void> {
  if (requesting.value || !sender) return
  const current = sender
  const attempt = ++ticket
  if (microphone.value) {
    stream?.getTracks().forEach((track) => track.stop())
    stream = null
    microphone.value = false
    await current.replaceTrack(null).catch(() => {})
    return
  }

  requesting.value = true
  let input: MediaStream | undefined
  try {
    input = await navigator.mediaDevices.getUserMedia({ audio: true })
    if (attempt !== ticket) return
    const track = input.getAudioTracks()[0]
    if (!track) throw new Error('No microphone track')
    await current.replaceTrack(track)
    if (attempt !== ticket) return
    stream = input
    microphone.value = true
  } catch {
    if (attempt === ticket)
      status.value = 'Не удалось включить микрофон. Проверьте доступ и устройство.'
  } finally {
    if (input && input !== stream) input.getTracks().forEach((track) => track.stop())
    if (attempt === ticket) requesting.value = false
  }
}

onBeforeUnmount(() => {
  unsubscribe()
  reset()
})
</script>

<template>
  <div v-show="available || status">
    <p v-if="status" role="status">{{ status }}</p>
    <button v-if="available" type="button" :disabled="requesting" @click="toggleMicrophone">
      {{ microphone ? 'Выключить микрофон' : 'Включить микрофон' }}
    </button>
    <audio v-show="available" ref="player" controls aria-label="Звук собеседника" />
  </div>
</template>

<style scoped>
audio {
  display: block;
  width: 100%;
}
</style>
