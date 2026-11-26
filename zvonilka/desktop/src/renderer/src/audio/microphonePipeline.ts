export const DEFAULT_HIGH_PASS_HZ = 90

export function clampHighPassHz(hz: number): number {
  return Math.max(60, Math.min(250, hz))
}

export type MicrophonePath = {
  context: AudioContext
  input: MediaStream
  output: MediaStream
  highPass: BiquadFilterNode
  gain: GainNode
  analyser: AnalyserNode
}

export async function createMicrophonePipeline(
  openInput: () => Promise<MediaStream>,
  settings: () => { gainPercent: number; highPassHz: number }
): Promise<MicrophonePath> {
  const context = new AudioContext()
  let input: MediaStream | undefined
  let output: MediaStream | undefined
  try {
    input = await openInput()
    const { gainPercent, highPassHz } = settings()
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
    source.connect(highPass).connect(gain).connect(analyser).connect(destination)
    await context.resume()
    return { context, input, output, highPass, gain, analyser }
  } catch (error) {
    input?.getTracks().forEach((track) => track.stop())
    output?.getTracks().forEach((track) => track.stop())
    void context.close().catch(() => {})
    throw error
  }
}

export function closeMicrophonePath(path: MicrophonePath | undefined): void {
  if (!path) return
  path.input.getTracks().forEach((track) => track.stop())
  path.output.getTracks().forEach((track) => track.stop())
  void path.context.close().catch(() => {})
}

export function updateMicrophoneGain(path: MicrophonePath, percent: number): void {
  path.gain.gain.setTargetAtTime(percent / 100, path.context.currentTime, 0.01)
}

export function updateMicrophoneHighPass(path: MicrophonePath, hz: number): void {
  path.highPass.frequency.setTargetAtTime(clampHighPassHz(hz), path.context.currentTime, 0.015)
}
