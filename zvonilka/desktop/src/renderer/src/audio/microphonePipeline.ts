import { prepareRnnoise } from './rnnoise'

export type NoiseMode = 'off' | 'standard' | 'rnnoise'

export const DEFAULT_HIGH_PASS_HZ = 90

export function clampHighPassHz(hz: number): number {
  return Math.max(60, Math.min(250, hz))
}

export type MicrophonePath = {
  context: AudioContext
  input: MediaStream
  output: MediaStream
  source: MediaStreamAudioSourceNode
  rnnoise?: { node: AudioNode; dispose: () => void }
  highPassEnabled: boolean
  highPass: BiquadFilterNode
  gain: GainNode
  analyser: AnalyserNode
}

export async function createMicrophonePipeline(
  openInput: (mode: NoiseMode) => Promise<MediaStream>,
  noiseMode: NoiseMode,
  reportRnnoiseFallback: (duringCapture: boolean) => void,
  settings: () => { gainPercent: number; highPassHz: number; highPassEnabled: boolean }
): Promise<MicrophonePath> {
  let enableRnnoise = noiseMode === 'rnnoise'
  let context: AudioContext
  try {
    context = new AudioContext({ sampleRate: 48000 })
  } catch (error) {
    if (enableRnnoise) reportRnnoiseFallback(false)
    console.warn('48 kHz microphone context unavailable', error)
    enableRnnoise = false
    context = new AudioContext()
  }
  let rnnoise: Awaited<ReturnType<typeof prepareRnnoise>> | undefined
  let input: MediaStream | undefined
  let output: MediaStream | undefined
  try {
    if (enableRnnoise) {
      try {
        rnnoise = await prepareRnnoise(context)
      } catch (error) {
        console.warn('RNNoise initialization failed', error)
        reportRnnoiseFallback(false)
      }
    }
    input = await openInput(rnnoise ? 'rnnoise' : noiseMode === 'off' ? 'off' : 'standard')
    const { gainPercent, highPassHz, highPassEnabled } = settings()
    const source = context.createMediaStreamSource(input)
    const highPass = context.createBiquadFilter()
    highPass.type = 'highpass'
    highPass.frequency.value = clampHighPassHz(highPassHz)
    const gain = context.createGain()
    gain.gain.value = gainPercent / 100
    const analyser = context.createAnalyser()
    analyser.fftSize = 2048
    const destination = context.createMediaStreamDestination()
    destination.channelCount = 1
    output = destination.stream
    const next = rnnoise?.node ?? gain
    if (highPassEnabled) source.connect(highPass).connect(next)
    else source.connect(next)
    rnnoise?.node.connect(gain)
    gain.connect(analyser).connect(destination)
    await context.resume()
    const path: MicrophonePath = {
      context,
      input,
      output,
      source,
      rnnoise,
      highPassEnabled,
      highPass,
      gain,
      analyser
    }
    rnnoise?.node.addEventListener(
      'processorerror',
      () => {
        if (!path.rnnoise || context.state === 'closed') return
        const upstream = path.highPassEnabled ? highPass : source
        upstream.disconnect(path.rnnoise.node)
        path.rnnoise.dispose()
        path.rnnoise = undefined
        upstream.connect(gain)
        reportRnnoiseFallback(true)
      },
      { once: true }
    )
    return path
  } catch (error) {
    rnnoise?.dispose()
    input?.getTracks().forEach((track) => track.stop())
    output?.getTracks().forEach((track) => track.stop())
    void context.close().catch(() => {})
    throw error
  }
}

export function closeMicrophonePath(path: MicrophonePath | undefined): void {
  if (!path) return
  path.rnnoise?.dispose()
  path.input.getTracks().forEach((track) => track.stop())
  path.output.getTracks().forEach((track) => track.stop())
  void path.context.close().catch(() => {})
}

export function setMicrophoneHighPassEnabled(path: MicrophonePath, enabled: boolean): void {
  if (path.highPassEnabled === enabled) return
  if (enabled) {
    path.source.disconnect(path.rnnoise?.node ?? path.gain)
    path.source.connect(path.highPass).connect(path.rnnoise?.node ?? path.gain)
  } else {
    path.source.disconnect(path.highPass)
    path.highPass.disconnect(path.rnnoise?.node ?? path.gain)
    path.source.connect(path.rnnoise?.node ?? path.gain)
  }
  path.highPassEnabled = enabled
}

export function updateMicrophoneGain(path: MicrophonePath, percent: number): void {
  path.gain.gain.setTargetAtTime(percent / 100, path.context.currentTime, 0.01)
}

export function updateMicrophoneHighPass(path: MicrophonePath, hz: number): void {
  path.highPass.frequency.setTargetAtTime(clampHighPassHz(hz), path.context.currentTime, 0.015)
}
