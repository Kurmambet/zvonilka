<script setup lang="ts">
import { onBeforeUnmount, ref, shallowRef } from 'vue'
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
  if (busy.value || !peers.value.length) return
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
      input = await navigator.mediaDevices.getUserMedia({ audio: true })
      if (attempt !== ticket) return
      if (!input.getAudioTracks().length) throw new Error('Microphone track missing')
      capture = input
      microphone.value = true
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
  } catch {
    if (attempt === ticket) microphoneError.value = 'Не удалось включить микрофон.'
  } finally {
    if (input && input !== capture) input.getTracks().forEach((track) => track.stop())
    if (attempt === ticket) busy.value = false
  }
}

onBeforeUnmount(() => {
  unsubscribe()
  update([])
})
</script>

<template>
  <div v-if="peers.length" class="audio-links">
    <button type="button" :disabled="busy" @click="toggleMicrophone">
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
