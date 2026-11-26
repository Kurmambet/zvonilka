<script setup lang="ts">
import { ref } from 'vue'

import type { NoiseMode } from '../audio/microphonePipeline'

defineProps<{
  microphones: MediaDeviceInfo[]
  outputs: MediaDeviceInfo[]
  outputId: string
  outputSupported: boolean
  outputNotice: string
  busy: boolean
  testing: boolean
  microphone: boolean
  level: number
  highPassEnabled: boolean
  echoCancellationEnabled: boolean
  noiseMode: NoiseMode
  rnnoiseNotice: string
  highPassHz: number
  microphoneGain: number
  microphoneOverload: boolean
  testRecordingUrl: string
  testPlaying: boolean
  testVolume: number
}>()

const selectedMicrophone = defineModel<string>({ required: true })
const emit = defineEmits<{
  output: [id: string]
  'refresh-devices': []
  player: [element: unknown]
  playing: [playing: boolean]
  'toggle-test': []
  'close-test': []
  'toggle-playback': []
  'restart-playback': []
  volume: [event: Event]
  'high-pass-enabled': [enabled: boolean]
  'echo-cancellation-enabled': [enabled: boolean]
  'noise-mode': [mode: NoiseMode]
  'high-pass-hz': [hz: number]
  'microphone-gain': [percent: number]
}>()

function changeOutput(event: Event): void {
  if (event.currentTarget instanceof HTMLSelectElement) emit('output', event.currentTarget.value)
}

function toggleHighPass(event: Event): void {
  if (event.currentTarget instanceof HTMLInputElement) {
    emit('high-pass-enabled', event.currentTarget.checked)
  }
}

function changeEchoCancellation(event: Event): void {
  if (event.currentTarget instanceof HTMLInputElement) {
    emit('echo-cancellation-enabled', event.currentTarget.checked)
  }
}

function changeNoiseMode(event: Event): void {
  if (!(event.currentTarget instanceof HTMLSelectElement)) return
  const mode = event.currentTarget.value
  if (mode === 'off' || mode === 'standard' || mode === 'rnnoise') emit('noise-mode', mode)
}

function changeHighPass(event: Event): void {
  if (event.currentTarget instanceof HTMLInputElement) {
    emit('high-pass-hz', event.currentTarget.valueAsNumber)
  }
}

function changeMicrophoneGain(event: Event): void {
  if (event.currentTarget instanceof HTMLInputElement) {
    emit('microphone-gain', event.currentTarget.valueAsNumber)
  }
}

const settingsDialog = ref<HTMLDialogElement | null>(null)

function openSoundSettings(): void {
  settingsDialog.value?.showModal()
  emit('refresh-devices')
}

function refreshDevices(): void {
  emit('refresh-devices')
}

function bindPlayer(element: unknown): void {
  emit('player', element)
}
</script>

<template>
  <div class="sound-launchers">
    <button type="button" @click="openSoundSettings">Настройки звука</button>
  </div>
  <label v-if="microphone" class="test-level">
    <span>Уровень микрофона</span>
    <meter :value="level" min="0" max="100" :class="{ 'is-peak': microphoneOverload }" />
    <span
      class="meter-peak"
      :class="{ 'is-visible': microphoneOverload }"
      title="Уменьшите усиление"
      >Пик</span
    >
  </label>
  <dialog
    ref="settingsDialog"
    class="sound-dialog sound-settings"
    aria-labelledby="sound-dialog-title"
    @close="emit('close-test')"
  >
    <header class="sound-dialog-header">
      <h4 id="sound-dialog-title">Настройки звука</h4>
      <button
        class="dialog-close"
        type="button"
        aria-label="Закрыть"
        @click="settingsDialog?.close()"
      >
        <span aria-hidden="true">×</span>
      </button>
    </header>
    <label class="device-field">
      <span>Устройство вывода</span>
      <select
        :value="outputId"
        :disabled="!outputSupported"
        @focus="refreshDevices"
        @pointerdown="refreshDevices"
        @change="changeOutput"
      >
        <option value="">Системное по умолчанию</option>
        <option v-for="(item, index) in outputs" :key="item.deviceId" :value="item.deviceId">
          {{ item.label || `Выход ${index + 1}` }}
        </option>
      </select>
    </label>
    <p v-if="!outputSupported" class="audio-caption">
      Этот клиент поддерживает только системный выход.
    </p>
    <p v-else-if="!outputs.length" class="audio-caption">
      Отдельные выходы не обнаружены. Подключите наушники или запустите проверку микрофона.
    </p>
    <p v-if="outputNotice" class="audio-caption" role="status">{{ outputNotice }}</p>
    <!-- Настройки обработки голоса добавляются рядом с выбором входа. -->
    <label class="device-field">
      <span>Устройство ввода</span>
      <select
        v-model="selectedMicrophone"
        :disabled="busy || testing || microphone"
        @focus="refreshDevices"
        @pointerdown="refreshDevices"
      >
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
    <details class="processing-settings">
      <summary>Обработка микрофона</summary>
      <label>
        <input
          type="checkbox"
          :checked="echoCancellationEnabled"
          :disabled="busy || testing || microphone"
          @change="changeEchoCancellation"
        />
        Эхоподавление
      </label>
      <label class="device-field">
        <span>Шумоподавление</span>
        <select
          :value="noiseMode"
          :disabled="busy || testing || microphone"
          @change="changeNoiseMode"
        >
          <option value="off">Выключено</option>
          <option value="standard">Стандартное</option>
          <option value="rnnoise">RNNoise</option>
        </select>
      </label>
      <p class="audio-caption">
        Эхоподавление и режим шумоподавления применяются при следующем включении микрофона.
      </p>
      <label>
        <input
          type="checkbox"
          :checked="highPassEnabled"
          :disabled="busy"
          @change="toggleHighPass"
        />
        Срез низких частот
      </label>
      <label class="device-field">
        <span>Срез низких частот · {{ highPassHz }} Гц</span>
        <input
          type="range"
          min="60"
          max="250"
          :value="highPassHz"
          :disabled="busy || !highPassEnabled"
          @input="changeHighPass"
        />
      </label>
      <label class="device-field">
        <span>Усиление микрофона · {{ microphoneGain }}%</span>
        <input
          type="range"
          min="50"
          max="200"
          :value="microphoneGain"
          :disabled="busy"
          @input="changeMicrophoneGain"
        />
      </label>
    </details>
    <p v-if="rnnoiseNotice" class="audio-caption" role="status">{{ rnnoiseNotice }}</p>
    <section class="microphone-test" aria-labelledby="microphone-test-title">
      <h4 id="microphone-test-title">Проверка микрофона</h4>
      <p v-if="microphone" class="audio-caption">Выключите микрофон звонка, чтобы начать тест.</p>
      <button type="button" :disabled="busy || microphone" @click="emit('toggle-test')">
        {{ testing ? 'Остановить и прослушать' : 'Начать запись' }}
      </button>

      <label v-if="testing" class="test-level">
        <span>Говорите в микрофон</span>
        <meter :value="level" min="0" max="100" :class="{ 'is-peak': microphoneOverload }" />
        <span
          class="meter-peak"
          :class="{ 'is-visible': microphoneOverload }"
          title="Уменьшите усиление"
          >Пик</span
        >
      </label>

      <div v-if="testRecordingUrl" class="audio-item">
        <p class="audio-caption">Тестовая запись · слышите только вы</p>
        <audio
          :ref="bindPlayer"
          :src="testRecordingUrl"
          hidden
          @play="emit('playing', true)"
          @pause="emit('playing', false)"
          @ended="emit('playing', false)"
        />
        <div class="audio-controls">
          <button type="button" :disabled="microphone || busy" @click="emit('toggle-playback')">
            {{ testPlaying ? 'Пауза' : 'Слушать' }}
          </button>
          <button type="button" :disabled="microphone || busy" @click="emit('restart-playback')">
            С начала
          </button>
          <input
            type="range"
            min="0"
            max="100"
            :value="testVolume"
            aria-label="Громкость тестовой записи"
            @input="emit('volume', $event)"
          />
        </div>
      </div>
    </section>
  </dialog>
</template>

<style scoped src="./SoundSettings.css"></style>
<style scoped>
.microphone-overload {
  color: #ffb4ab;
}
</style>
