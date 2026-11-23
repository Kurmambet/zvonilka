<script setup lang="ts">
defineProps<{
  microphones: MediaDeviceInfo[]
  busy: boolean
  testing: boolean
  microphone: boolean
  level: number
  testRecordingUrl: string
  testPlaying: boolean
  testVolume: number
}>()

const selectedMicrophone = defineModel<string>({ required: true })
const emit = defineEmits<{
  player: [element: unknown]
  playing: [playing: boolean]
  'toggle-test': []
  'toggle-playback': []
  'restart-playback': []
  volume: [event: Event]
}>()

function bindPlayer(element: unknown): void {
  emit('player', element)
}
</script>

<template>
  <details class="sound-settings">
    <summary>Настройки звука</summary>
    <!-- Настройки обработки голоса добавляются рядом с выбором входа. -->
    <label class="device-field">
      <span>Устройство ввода</span>
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
    <button type="button" :disabled="busy || microphone" @click="emit('toggle-test')">
      {{ testing ? 'Остановить и прослушать' : 'Проверить микрофон' }}
    </button>

    <label v-if="testing" class="test-level">
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
