<script setup lang="ts">
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
  'toggle-playback': []
  'restart-playback': []
  volume: [event: Event]
  'high-pass-enabled': [enabled: boolean]
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

function refreshOnOpen(event: Event): void {
  if (event.currentTarget instanceof HTMLDetailsElement && event.currentTarget.open) {
    emit('refresh-devices')
  }
}

function refreshDevices(): void {
  emit('refresh-devices')
}

function bindPlayer(element: unknown): void {
  emit('player', element)
}
</script>

<template>
  <details class="sound-settings" @toggle="refreshOnOpen">
    <summary>Настройки звука</summary>
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
    <label>
      <input type="checkbox" :checked="highPassEnabled" :disabled="busy" @change="toggleHighPass" />
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
    <p v-if="microphoneOverload" class="microphone-overload" role="status">
      Сигнал перегружен - уменьши усиление
    </p>
    <button type="button" :disabled="busy || microphone" @click="emit('toggle-test')">
      {{ testing ? 'Остановить и прослушать' : 'Проверить микрофон' }}
    </button>

    <label v-if="testing || microphone" class="test-level">
      <span>Говорите в микрофон</span>
      <meter :value="level" min="0" max="100" />
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
  </details>
</template>

<style scoped src="./SoundSettings.css"></style>
<style scoped>
.microphone-overload {
  color: #ffb4ab;
}
</style>
