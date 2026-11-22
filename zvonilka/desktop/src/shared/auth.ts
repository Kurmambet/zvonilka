export interface Profile {
  id: string
  username: string
  display_name: string
  created_at: string
}

export type LoginError =
  | 'invalid_credentials'
  | 'invalid_request'
  | 'rate_limited'
  | 'api_unavailable'
  | 'profile_failed'
  | 'unexpected'

export type LoginResult = { ok: true; profile: Profile } | { ok: false; error: LoginError }

export interface DesktopApi {
  sendAudio: (
    peer: import('./audio').AudioPeer,
    signal: import('./audio').AudioSignal
  ) => Promise<boolean>
  onAudio: (listener: (event: import('./audio').AudioEvent) => void) => () => void
  login(
    profileId: import('./server').ServerProfileId,
    username: string,
    password: string
  ): Promise<LoginResult>
  createRoom(): Promise<RoomResult<RoomSummary>>
  issueInvitation(roomId: string): Promise<RoomResult<string>>
  redeemInvitation(invitation: string): Promise<RoomResult<RoomSummary>>
  closeRoom(roomId: string): Promise<RoomResult<null>>
  register(
    profileId: import('./server').ServerProfileId,
    username: string,
    displayName: string,
    password: string
  ): Promise<RegisterResult>
  logout(): Promise<LogoutResult>
  getRoom(roomId: string): Promise<RoomResult<RoomSummary>>
  connectRoom(roomId: string): Promise<RoomResult<null>>
  disconnectRoom(): Promise<void>
  onPresence(listener: (state: PresenceState) => void): () => void
  probeServer(id: import('./server').ServerProfileId): Promise<import('./server').ServerProbeResult>
}

export interface RoomSummary {
  room_id: string
  state: 'created' | 'active' | 'recovering' | 'closed'
  owner_id: string
  memberCount: number
}

export type RoomError =
  | 'sign_in_required'
  | 'invalid_request'
  | 'api_unavailable'
  | 'forbidden'
  | 'not_found'
  | 'invite_expired'
  | 'invite_revoked'
  | 'invite_consumed'
  | 'rate_limited'
  | 'conflict'
  | 'unexpected'
  | 'owner_cannot_redeem'
  | 'room_closed'
  | 'refresh_uncertain'
  | 'invite_limit'

export type RoomResult<T> = { ok: true; value: T } | { ok: false; error: RoomError }

export type RegisterError =
  'invalid_request' | 'conflict' | 'rate_limited' | 'api_unavailable' | 'unexpected'

export type RegisterResult = { ok: true; username: string } | { ok: false; error: RegisterError }

export type LogoutResult = 'revoked' | 'no_local_session' | 'not_confirmed'

export type PresenceState =
  | { phase: 'idle' }
  | { phase: 'connecting'; roomId: string }
  | { phase: 'online'; roomId: string; userIds: string[] }
  | { phase: 'ended'; roomId: string; reason: string }
