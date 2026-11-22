import type { LoginResult, RegisterResult, LogoutResult } from '../shared/auth'

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

export interface TokenPair {
  accessToken: string
  refreshToken: string
  sessionId: string
}

type RefreshResult =
  { ok: true; tokens: TokenPair } | { ok: false; reason: 'rejected' | 'rate_limited' | 'uncertain' }

function parseTokenPair(data: unknown): TokenPair | null {
  if (
    !isRecord(data) ||
    typeof data.access_token !== 'string' ||
    !data.access_token ||
    typeof data.refresh_token !== 'string' ||
    data.refresh_token.length !== 43 ||
    typeof data.session_id !== 'string' ||
    !data.session_id
  ) {
    return null
  }

  return {
    accessToken: data.access_token,
    refreshToken: data.refresh_token,
    sessionId: data.session_id
  }
}

export async function refreshSession(
  apiBaseUrl: string,
  current: TokenPair
): Promise<RefreshResult> {
  try {
    const response = await fetch(`${apiBaseUrl}/api/v1/auth/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: current.refreshToken }),
      redirect: 'error',
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS)
    })

    if (response.status === 401 || response.status === 422) {
      return { ok: false, reason: 'rejected' }
    }

    if (response.status === 429) return { ok: false, reason: 'rate_limited' }
    if (response.status !== 200) return { ok: false, reason: 'uncertain' }

    const tokens = parseTokenPair(await readJson(response))
    if (!tokens || tokens.sessionId !== current.sessionId) {
      return { ok: false, reason: 'uncertain' }
    }

    return { ok: true, tokens }
  } catch {
    return { ok: false, reason: 'uncertain' }
  }
}

export async function loginAndLoadProfile(
  apiBaseUrl: string,
  username: unknown,
  password: unknown,

  onAuthenticated: (tokens: TokenPair) => void
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
    const login = await fetch(`${apiBaseUrl}/api/v1/auth/login`, {
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

    // Вместо старого чтения и проверки access_token:
    const tokens = parseTokenPair(await readJson(login))
    if (!tokens) return { ok: false, error: 'unexpected' }

    stage = 'profile'
    const me = await fetch(`${apiBaseUrl}/api/v1/users/me`, {
      headers: { Authorization: `Bearer ${tokens.accessToken}` },
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

    onAuthenticated(tokens)

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

export { REQUEST_TIMEOUT_MS, isRecord, readJson }

export async function registerAccount(
  apiBaseUrl: string,
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
    const response = await fetch(`${apiBaseUrl}/api/v1/auth/register`, {
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

export async function revokeCurrentSession(
  apiBaseUrl: string,
  accessToken: string
): Promise<LogoutResult> {
  try {
    const response = await fetch(`${apiBaseUrl}/api/v1/auth/logout`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${accessToken}` },
      redirect: 'error',
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS)
    })

    return response.status === 204 ? 'revoked' : 'not_confirmed'
  } catch {
    return 'not_confirmed'
  }
}
