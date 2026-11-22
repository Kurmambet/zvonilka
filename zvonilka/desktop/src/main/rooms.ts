import type { RoomError, RoomResult, RoomSummary } from '../shared/auth'
import { REQUEST_TIMEOUT_MS, isRecord, readJson } from './auth'

const UUID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i

function parseRoom(data: unknown): RoomSummary | null {
  if (!isRecord(data) || typeof data.room_id !== 'string' || !UUID.test(data.room_id)) {
    return null
  }

  if (typeof data.owner_id !== 'string' || !UUID.test(data.owner_id)) return null
  const { state } = data
  if (state !== 'created' && state !== 'active' && state !== 'recovering' && state !== 'closed') {
    return null
  }

  if (!Array.isArray(data.members)) return null

  return {
    room_id: data.room_id,
    owner_id: data.owner_id,
    state,
    memberCount: data.members.length
  }
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
    if (
      code === 'room_closed' ||
      code === 'invite_expired' ||
      code === 'invite_revoked' ||
      code === 'invite_consumed' ||
      code === 'invite_limit'
    ) {
      return code
    }
    if (response.status === 404) return 'not_found'
    return 'conflict'
  }

  return 'unexpected'
}

async function requestRoom<T>(
  apiBaseUrl: string,
  accessToken: string,
  method: 'GET' | 'POST',
  path: string,
  body: Record<string, string> | undefined,
  parse: (data: unknown) => T | null
): Promise<RoomResult<T>> {
  const headers: Record<string, string> = { Authorization: `Bearer ${accessToken}` }
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  try {
    const response = await fetch(`${apiBaseUrl}${path}`, {
      method: method,
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

export type IceServer = { urls: string[]; username?: string; credential?: string }

export function getIceServers(
  apiBaseUrl: string,
  accessToken: string
): Promise<RoomResult<IceServer[]>> {
  return requestRoom(apiBaseUrl, accessToken, 'GET', '/api/v1/ice-servers', undefined, (data) => {
    if (!isRecord(data) || !Array.isArray(data.iceServers)) return null
    const result: IceServer[] = []
    for (const item of data.iceServers as unknown[]) {
      if (!isRecord(item) || !Array.isArray(item.urls) || item.urls.length === 0) return null
      const urls: string[] = []
      for (const url of item.urls as unknown[]) {
        if (typeof url !== 'string' || !/^(stun|turn|turns):/.test(url)) return null
        urls.push(url)
      }
      if (urls.some((url) => /^turns?:/.test(url))) {
        if (typeof item.username !== 'string' || typeof item.credential !== 'string') return null
        result.push({ urls, username: item.username, credential: item.credential })
      } else {
        result.push({ urls })
      }
    }
    return result
  })
}

export function createRoom(
  apiBaseUrl: string,
  accessToken: string
): Promise<RoomResult<RoomSummary>> {
  return requestRoom(apiBaseUrl, accessToken, 'POST', '/api/v1/rooms', {}, parseRoom)
}

export async function issueInvitation(
  apiBaseUrl: string,
  accessToken: string,
  roomId: unknown
): Promise<RoomResult<string>> {
  if (typeof roomId !== 'string' || !UUID.test(roomId)) {
    return { ok: false, error: 'invalid_request' }
  }

  return requestRoom(
    apiBaseUrl,
    accessToken,
    'POST',
    `/api/v1/rooms/${roomId}/invites`,
    undefined,
    (data) => {
      if (
        !isRecord(data) ||
        data.room_id !== roomId ||
        typeof data.invitation !== 'string' ||
        data.invitation.length !== 84
      ) {
        return null
      }

      return data.invitation
    }
  )
}

export async function redeemInvitation(
  apiBaseUrl: string,
  accessToken: string,
  input: unknown
): Promise<RoomResult<RoomSummary>> {
  if (typeof input !== 'string' || input.length > 256) {
    return { ok: false, error: 'invalid_request' }
  }

  const invitation = input.trim()
  if (invitation.length !== 84) return { ok: false, error: 'invalid_request' }

  return requestRoom(
    apiBaseUrl,
    accessToken,
    'POST',
    '/api/v1/invites/redeem',
    { invitation },
    parseRoom
  )
}

export async function closeRoom(
  apiBaseUrl: string,
  accessToken: string,
  roomId: unknown
): Promise<RoomResult<null>> {
  if (typeof roomId !== 'string' || !UUID.test(roomId)) {
    return { ok: false, error: 'invalid_request' }
  }

  try {
    const response = await fetch(`${apiBaseUrl}/api/v1/rooms/${roomId}`, {
      method: 'DELETE',
      headers: { Authorization: `Bearer ${accessToken}` },
      redirect: 'error',
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS)
    })

    if (response.status === 204) return { ok: true, value: null }

    return { ok: false, error: await responseError(response) }
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

export async function getRoom(
  apiBaseUrl: string,
  accessToken: string,
  input: unknown
): Promise<RoomResult<RoomSummary>> {
  if (typeof input !== 'string' || !UUID.test(input.trim())) {
    return { ok: false, error: 'invalid_request' }
  }

  return requestRoom(
    apiBaseUrl,
    accessToken,
    'GET',
    `/api/v1/rooms/${input.trim().toLowerCase()}`,
    undefined,
    parseRoom
  )
}
