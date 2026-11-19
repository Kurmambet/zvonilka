import type { LoginResult, RegisterResult } from '../shared/auth'

const API_BASE_URL = 'http://127.0.0.1:8001'
const REQUEST_TIMEOUT_MS = 5000

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

async function readJson(response: Response): Promise<unknown> {
  try {
    const data: unknown = await response.json()
    return data
  } catch (error) {
    if (error instanceof SyntaxError) return null
    throw error
  }
}

export async function loginAndLoadProfile(
  username: unknown,
  password: unknown,
  onAuthenticated: (accessToken: string) => void
): Promise<LoginResult> {
  if (
    typeof username !== 'string' ||
    username.trim().length === 0 ||
    username.length > 64 ||
    typeof password !== 'string' ||
    password.length === 0 ||
    password.length > 128
  ) {
    return { ok: false, error: 'invalid_request' }
  }

  let stage: 'login' | 'profile' = 'login'

  try {
    const login = await fetch(`${API_BASE_URL}/api/v1/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
      redirect: 'error',
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS)
    })

    if (login.status === 401) return { ok: false, error: 'invalid_credentials' }
    if (login.status === 422) return { ok: false, error: 'invalid_request' }
    if (login.status === 429) return { ok: false, error: 'rate_limited' }
    if (login.status === 503) return { ok: false, error: 'api_unavailable' }
    if (!login.ok) return { ok: false, error: 'unexpected' }

    const tokens = await readJson(login)
    if (!isRecord(tokens) || typeof tokens.access_token !== 'string' || !tokens.access_token) {
      return { ok: false, error: 'unexpected' }
    }

    stage = 'profile'
    const me = await fetch(`${API_BASE_URL}/api/v1/users/me`, {
      headers: { Authorization: `Bearer ${tokens.access_token}` },
      redirect: 'error',
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS)
    })

    if (me.status === 503) return { ok: false, error: 'api_unavailable' }
    if (!me.ok) return { ok: false, error: 'profile_failed' }

    const profile = await readJson(me)
    if (
      !isRecord(profile) ||
      typeof profile.id !== 'string' ||
      typeof profile.username !== 'string' ||
      typeof profile.display_name !== 'string' ||
      typeof profile.created_at !== 'string'
    ) {
      return { ok: false, error: 'profile_failed' }
    }

    onAuthenticated(tokens.access_token)

    return {
      ok: true,
      profile: {
        id: profile.id,
        username: profile.username,
        display_name: profile.display_name,
        created_at: profile.created_at
      }
    }
  } catch (error) {
    if (
      error instanceof TypeError ||
      (error instanceof Error && ['TimeoutError', 'AbortError'].includes(error.name))
    ) {
      return { ok: false, error: 'api_unavailable' }
    }
    return { ok: false, error: stage === 'profile' ? 'profile_failed' : 'unexpected' }
  }
}

export { API_BASE_URL, REQUEST_TIMEOUT_MS, isRecord, readJson }

export async function registerAccount(
  username: unknown,
  displayName: unknown,
  password: unknown
): Promise<RegisterResult> {
  if (
    typeof username !== 'string' ||
    typeof displayName !== 'string' ||
    typeof password !== 'string' ||
    [...username].length < 3 ||
    [...username].length > 64 ||
    !displayName.trim() ||
    [...displayName].length > 128 ||
    [...password].length < 12 ||
    [...password].length > 128
  ) {
    return { ok: false, error: 'invalid_request' }
  }

  try {
    const response = await fetch(`${API_BASE_URL}/api/v1/auth/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, display_name: displayName, password }),
      redirect: 'error',
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS)
    })

    if (response.status === 409) return { ok: false, error: 'conflict' }
    if (response.status === 422) return { ok: false, error: 'invalid_request' }
    if (response.status === 429) return { ok: false, error: 'rate_limited' }
    if (response.status === 503) return { ok: false, error: 'api_unavailable' }
    if (response.status !== 201) return { ok: false, error: 'unexpected' }

    const profile = await readJson(response)
    if (!isRecord(profile) || typeof profile.username !== 'string' || !profile.username) {
      return { ok: false, error: 'unexpected' }
    }

    return { ok: true, username: profile.username }
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
