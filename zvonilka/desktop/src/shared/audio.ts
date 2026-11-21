export type AudioSignal = { type: 'offer' | 'answer'; sdp: string }
export interface AudioPeer {
  roomId: string
  userId: string
  generation: string
  initiator: boolean
}
export type AudioEvent = { peer: AudioPeer | null } | { peer: AudioPeer; signal: AudioSignal }
