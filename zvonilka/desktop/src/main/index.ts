import { app, BrowserWindow, ipcMain } from 'electron'
import { pathToFileURL } from 'node:url'
import { loginAndLoadProfile, registerAccount, revokeCurrentSession, refreshSession } from './auth'
import type { TokenPair } from './auth'
import type { LoginResult, RegisterResult, LogoutResult } from '../shared/auth'
import { join } from 'path'
import { electronApp, optimizer, is } from '@electron-toolkit/utils'
import icon from '../../resources/icon.png?asset'
import type { IpcMainInvokeEvent, WebContents } from 'electron'
import { createRoom, issueInvitation, redeemInvitation, closeRoom, getRoom } from './rooms'
import type { RoomResult, PresenceState } from '../shared/auth'
import { connectPresence } from './presence'
import type { ServerProfileId, ServerProbeResult } from '../shared/server'

const SERVER_URLS: Record<ServerProfileId, string> = {
  local: 'http://127.0.0.1:8001',
  staging: 'https://api.zvonilka.website'
}

function rendererUrl(): string {
  const devUrl = is.dev ? process.env['ELECTRON_RENDERER_URL'] : undefined
  return devUrl
    ? new URL(devUrl).href
    : pathToFileURL(join(__dirname, '../renderer/index.html')).href
}

interface WindowSession {
  tokens: TokenPair
  refreshing?: ReturnType<typeof refreshSession>
  refreshUncertain?: boolean
}

const sessions = new WeakMap<WebContents, WindowSession>()
const loginAttempts = new WeakMap<WebContents, object>()

type PresenceConnection = Partial<ReturnType<typeof connectPresence>>
const presenceByWindow = new WeakMap<WebContents, PresenceConnection>()

function stopPresence(sender: WebContents): void {
  const connection = presenceByWindow.get(sender)
  connection?.stop?.()
  presenceByWindow.delete(sender)
}

function clearSession(sender: WebContents): void {
  stopPresence(sender)
  sessions.delete(sender)
}

function isTrustedRenderer(event: IpcMainInvokeEvent): boolean {
  const frame = event.senderFrame
  return frame !== null && frame === event.sender.mainFrame && frame.url === rendererUrl()
}

async function refreshForWindow(
  sender: WebContents,
  session: WindowSession,
  previous: TokenPair
): Promise<RoomResult<string>> {
  if (sessions.get(sender) !== session) return { ok: false, error: 'unexpected' }

  if (session.tokens !== previous) {
    return { ok: true, value: session.tokens.accessToken }
  }

  if (session.refreshUncertain) return { ok: false, error: 'refresh_uncertain' }

  const pending = (session.refreshing ??= refreshSession(previous))
  const result = await pending

  if (sessions.get(sender) !== session) return { ok: false, error: 'unexpected' }

  if (session.refreshing === pending) {
    session.refreshing = undefined
    if (result.ok) session.tokens = result.tokens
    else if (result.reason === 'uncertain') session.refreshUncertain = true
  }

  if (result.ok) return { ok: true, value: session.tokens.accessToken }
  if (result.reason === 'rate_limited') return { ok: false, error: 'rate_limited' }
  if (result.reason === 'uncertain') return { ok: false, error: 'refresh_uncertain' }
  return { ok: false, error: 'sign_in_required' }
}

async function withAccessToken<T>(
  event: IpcMainInvokeEvent,
  action: (token: string) => Promise<RoomResult<T>>
): Promise<RoomResult<T>> {
  if (!isTrustedRenderer(event)) return { ok: false, error: 'unexpected' }

  const sender = event.sender
  const session = sessions.get(sender)
  if (!session) return { ok: false, error: 'sign_in_required' }

  const initialTokens = session.tokens
  const first = await action(initialTokens.accessToken)

  // Не возвращаем результаты старого аккаунта после logout или нового login.
  if (sessions.get(sender) !== session) return { ok: false, error: 'unexpected' }
  if (first.ok || first.error !== 'sign_in_required') return first

  const refreshed = await refreshForWindow(sender, session, initialTokens)
  if (sessions.get(sender) !== session) return { ok: false, error: 'unexpected' }

  if (!refreshed.ok) {
    if (refreshed.error === 'sign_in_required') clearSession(sender)
    return refreshed
  }

  const retryTokens = session.tokens
  const retry = await action(retryTokens.accessToken)

  if (sessions.get(sender) !== session) return { ok: false, error: 'unexpected' }
  if (!retry.ok && retry.error === 'sign_in_required' && session.tokens === retryTokens) {
    clearSession(sender)
  }

  return retry
}

function createWindow(): void {
  // Create the browser window.
  const mainWindow = new BrowserWindow({
    width: 900,
    height: 670,
    show: false,
    autoHideMenuBar: true,
    ...(process.platform === 'linux' ? { icon } : {}),
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      webSecurity: true,
      sandbox: true
    }
  })

  mainWindow.on('ready-to-show', () => {
    mainWindow.show()
  })

  mainWindow.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  mainWindow.webContents.on('will-navigate', (event) => event.preventDefault())

  const sender = mainWindow.webContents
  sender.once('destroyed', () => {
    loginAttempts.delete(sender)
    clearSession(sender)
  })

  void mainWindow.loadURL(rendererUrl())
  // HMR for renderer base on electron-vite cli.
  // Load the remote URL for development or the local html file for production.
}

// This method will be called when Electron has finished
// initialization and is ready to create browser windows.
// Some APIs can only be used after this event occurs.
app.whenReady().then(() => {
  // Set app user model id for windows
  electronApp.setAppUserModelId('com.electron')

  // Default open or close DevTools by F12 in development
  // and ignore CommandOrControl + R in production.
  // see https://github.com/alex8088/electron-toolkit/tree/master/packages/utils
  app.on('browser-window-created', (_, window) => {
    optimizer.watchWindowShortcuts(window)
  })

  // IPC test
  ipcMain.handle(
    'auth:login',
    async (event, username: unknown, password: unknown): Promise<LoginResult> => {
      if (!isTrustedRenderer(event)) return { ok: false, error: 'unexpected' }

      const sender = event.sender
      const attempt = {}
      loginAttempts.set(sender, attempt)
      clearSession(sender)

      try {
        const result = await loginAndLoadProfile(username, password, (tokens) => {
          if (!sender.isDestroyed() && loginAttempts.get(sender) === attempt) {
            sessions.set(sender, { tokens })
          }
        })

        return loginAttempts.get(sender) === attempt ? result : { ok: false, error: 'unexpected' }
      } finally {
        if (loginAttempts.get(sender) === attempt) loginAttempts.delete(sender)
      }
    }
  )

  ipcMain.handle('server:probe', async (event, id: unknown): Promise<ServerProbeResult> => {
    if (!isTrustedRenderer(event) || (id !== 'local' && id !== 'staging')) {
      return { ok: false }
    }

    const started = performance.now()

    try {
      const response = await fetch(new URL('/health/live', SERVER_URLS[id]), {
        cache: 'no-store',
        redirect: 'error',
        signal: AbortSignal.timeout(3000)
      })

      if (!response.ok) return { ok: false }

      const body: unknown = await response.json()
      if (
        typeof body !== 'object' ||
        body === null ||
        !('status' in body) ||
        body.status !== 'ok'
      ) {
        return { ok: false }
      }

      return { ok: true, latencyMs: Math.round(performance.now() - started) }
    } catch {
      return { ok: false }
    }
  })

  ipcMain.handle('rooms:create', (event) => withAccessToken(event, createRoom))

  ipcMain.handle('rooms:issue-invitation', (event, roomId: unknown) =>
    withAccessToken(event, (token) => issueInvitation(token, roomId))
  )

  ipcMain.handle('rooms:redeem-invitation', (event, invitation: unknown) =>
    withAccessToken(event, (token) => redeemInvitation(token, invitation))
  )

  ipcMain.handle('rooms:close', (event, roomId: unknown) =>
    withAccessToken(event, (token) => closeRoom(token, roomId))
  )

  ipcMain.handle('rooms:get', (event, roomId: unknown) =>
    withAccessToken(event, (token) => getRoom(token, roomId))
  )

  ipcMain.handle(
    'auth:register',
    async (
      event,
      username: unknown,
      displayName: unknown,
      password: unknown
    ): Promise<RegisterResult> => {
      if (!isTrustedRenderer(event)) return { ok: false, error: 'unexpected' }
      return registerAccount(username, displayName, password)
    }
  )

  ipcMain.handle('auth:logout', async (event): Promise<LogoutResult> => {
    if (!isTrustedRenderer(event)) return 'not_confirmed'

    const sender = event.sender
    const session = sessions.get(sender)
    loginAttempts.delete(sender)
    clearSession(sender)

    if (!session) return 'no_local_session'

    const refreshed = await session.refreshing
    const token = refreshed?.ok ? refreshed.tokens.accessToken : session.tokens.accessToken
    return revokeCurrentSession(token)
  })

  ipcMain.handle('presence:connect', (event, input: unknown): RoomResult<null> => {
    if (!isTrustedRenderer(event)) return { ok: false, error: 'unexpected' }

    if (
      typeof input !== 'string' ||
      !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(input)
    ) {
      return { ok: false, error: 'invalid_request' }
    }

    const sender = event.sender
    const session = sessions.get(sender)
    if (!session) return { ok: false, error: 'sign_in_required' }

    stopPresence(sender)
    const connection: PresenceConnection = {}
    presenceByWindow.set(sender, connection)

    let socketTokens = session.tokens

    Object.assign(
      connection,
      connectPresence(
        input.toLowerCase(),
        socketTokens.accessToken,
        async () => {
          if (presenceByWindow.get(sender) !== connection) {
            return { ok: false, error: 'unexpected' }
          }

          const result = await refreshForWindow(sender, session, socketTokens)
          if (result.ok) socketTokens = session.tokens
          return result
        },
        (state: PresenceState) => {
          if (sender.isDestroyed() || presenceByWindow.get(sender) !== connection) return
          sender.send('presence:state', state)

          if (state.phase === 'ended') {
            presenceByWindow.delete(sender)
            if (state.reason === 'sign_in_required' && sessions.get(sender) === session) {
              clearSession(sender)
            }
          }
        },
        (audio) => {
          if (!sender.isDestroyed() && presenceByWindow.get(sender) === connection) {
            sender.send('audio:event', audio)
          }
        }
      )
    )

    return { ok: true, value: null }
  })

  ipcMain.handle('audio:send', (event, peer: unknown, signal: unknown): boolean => {
    if (!isTrustedRenderer(event) || !sessions.has(event.sender)) return false
    return presenceByWindow.get(event.sender)?.sendAudio?.(peer, signal) ?? false
  })

  ipcMain.handle('presence:disconnect', (event): void => {
    if (!isTrustedRenderer(event)) return
    stopPresence(event.sender)
    event.sender.send('presence:state', { phase: 'idle' } satisfies PresenceState)
  })

  createWindow()

  app.on('activate', function () {
    // On macOS it's common to re-create a window in the app when the
    // dock icon is clicked and there are no other windows open.
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

// Quit when all windows are closed, except on macOS. There, it's common
// for applications and their menu bar to stay active until the user quits
// explicitly with Cmd + Q.
app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit()
  }
})

// In this file you can include the rest of your app's specific main process
// code. You can also put them in separate files and require them here.
