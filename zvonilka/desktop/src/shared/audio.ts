export type AudioSignal = { type: 'offer' | 'answer'; sdp: string }
export interface AudioPeer {
  roomId: string
  userId: string
  generation: string
  initiator: boolean
  displayName?: string
}
export type AudioEvent =
  | { peer: AudioPeer | null }
  | { peers: AudioPeer[] }
  | { peer: AudioPeer; signal: AudioSignal }
  | { wsDiagnostic: WsDiagnostic }

export type WsDiagnostic = {
  at: string
  event:
    | 'ws.started'
    | 'ws.attempt'
    | 'ws.open'
    | 'ws.joined'
    | 'ws.heartbeat-ack'
    | 'ws.auth-retry'
    | 'ws.error'
    | 'ws.ended'
    | 'ws.close'
  authRequestId?: string
  generation?: string
  reason?: string
  closeCode?: number
  wasClean?: boolean
}
