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
  login(username: string, password: string): Promise<LoginResult>
  createRoom(): Promise<RoomResult<RoomSummary>>
  issueInvitation(roomId: string): Promise<RoomResult<string>>
  redeemInvitation(invitation: string): Promise<RoomResult<RoomSummary>>
  closeRoom(roomId: string): Promise<RoomResult<null>>
  register(username: string, displayName: string, password: string): Promise<RegisterResult>
}

export interface RoomSummary {
  room_id: string
  state: 'created' | 'active' | 'recovering' | 'closed'
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

export type RoomResult<T> = { ok: true; value: T } | { ok: false; error: RoomError }

export type RegisterError =
  'invalid_request' | 'conflict' | 'rate_limited' | 'api_unavailable' | 'unexpected'

export type RegisterResult = { ok: true; username: string } | { ok: false; error: RegisterError }
