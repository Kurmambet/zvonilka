import { loadRnnoise, RnnoiseWorkletNode } from '@sapphi-red/web-noise-suppressor'
import workletUrl from '@sapphi-red/web-noise-suppressor/rnnoiseWorklet.js?url&no-inline'
import wasmUrl from '@sapphi-red/web-noise-suppressor/rnnoise.wasm?url&no-inline'
import simdWasmUrl from '@sapphi-red/web-noise-suppressor/rnnoise_simd.wasm?url&no-inline'

export async function prepareRnnoise(context: AudioContext): Promise<{
  node: RnnoiseWorkletNode
  dispose: () => void
}> {
  const [wasmBinary] = await Promise.all([
    loadRnnoise({ url: wasmUrl, simdUrl: simdWasmUrl }),
    context.audioWorklet.addModule(workletUrl)
  ])
  const node = new RnnoiseWorkletNode(context, { wasmBinary, maxChannels: 1 })
  node.channelCount = 1
  node.channelCountMode = 'explicit'
  return {
    node,
    dispose: () => {
      node.disconnect()
      node.destroy()
    }
  }
}
