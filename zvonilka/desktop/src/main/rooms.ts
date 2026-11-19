import type { RoomError, RoomResult, RoomSummary } from '../shared/auth'
import { API_BASE_URL, REQUEST_TIMEOUT_MS, isRecord, readJson } from './auth'

const UUID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i

function parseRoom(data: unknown): RoomSummary | null {
  if (!isRecord(data) || typeof data.room_id !== 'string' || !UUID.test(data.room_id)) {
    return null
  }

  const { state } = data
  if (state !== 'created' && state !== 'active' && state !== 'recovering' && state !== 'closed') {
    return null
  }

  return { room_id: data.room_id, state }
}

async function responseError(response: Response): Promise<RoomError> {
  switch (response.status) {
    case 401:
      return 'sign_in_required'
    case 403: {
      const payload = await readJson(response)
      if (
        isRecord(payload) &&
        isRecord(payload.error) &&
        payload.error.code === 'owner_cannot_redeem'
      ) {
        return 'owner_cannot_redeem'
      }
      return 'forbidden'
    }
    case 422:
      return 'invalid_request'
    case 429:
      return 'rate_limited'
    case 503:
      return 'api_unavailable'
  }

  if (response.status === 404 || response.status === 409 || response.status === 410) {
    const payload = await readJson(response)
    const code = isRecord(payload) && isRecord(payload.error) ? payload.error.code : undefined
    if (code === 'invite_expired' || code === 'invite_revoked' || code === 'invite_consumed') {
      return code
    }
    if (response.status === 404) return 'not_found'
    return 'conflict'
  }

  return 'unexpected'
}

async function post<T>(
  accessToken: string,
  path: string,
  body: Record<string, string> | undefined,
  parse: (data: unknown) => T | null
): Promise<RoomResult<T>> {
  const headers: Record<string, string> = { Authorization: `Bearer ${accessToken}` }
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  try {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      method: 'POST',
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      redirect: 'error',
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS)
    })

    if (!response.ok) return { ok: false, error: await responseError(response) }

    const value = parse(await readJson(response))
    return value === null ? { ok: false, error: 'unexpected' } : { ok: true, value }
  } catch (error) {
    if (
      error instanceof TypeError ||
      (error instanceof Error && ['TimeoutError', 'AbortError'].includes(error.name))
    ) {
      return { ok: false, error: 'api_unavailable' }
    }

    return { ok: false, error: 'unexpected' }
  }
}

export function createRoom(accessToken: string): Promise<RoomResult<RoomSummary>> {
  return post(accessToken, '/api/v1/rooms', {}, parseRoom)
}

export async function issueInvitation(
  accessToken: string,
  roomId: unknown
): Promise<RoomResult<string>> {
  if (typeof roomId !== 'string' || !UUID.test(roomId)) {
    return { ok: false, error: 'invalid_request' }
  }

  return post(accessToken, `/api/v1/rooms/${roomId}/invites`, undefined, (data) => {
    if (
      !isRecord(data) ||
      data.room_id !== roomId ||
      typeof data.invitation !== 'string' ||
      data.invitation.length !== 84
    ) {
      return null
    }

    return data.invitation
  })
}

export async function redeemInvitation(
  accessToken: string,
  input: unknown
): Promise<RoomResult<RoomSummary>> {
  if (typeof input !== 'string' || input.length > 256) {
    return { ok: false, error: 'invalid_request' }
  }

  const invitation = input.trim()
  if (invitation.length !== 84) return { ok: false, error: 'invalid_request' }

  return post(accessToken, '/api/v1/invites/redeem', { invitation }, parseRoom)
}
