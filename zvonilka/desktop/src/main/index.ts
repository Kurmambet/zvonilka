import { app, BrowserWindow, ipcMain } from 'electron'
import { pathToFileURL } from 'node:url'
import {
  loginAndLoadProfile,
  registerAccount,
  revokeCurrentSession,
  refreshSession,
  TokenPair
} from './auth'
import type { LoginResult, RegisterResult, LogoutResult } from '../shared/auth'
import { join } from 'path'
import { electronApp, optimizer, is } from '@electron-toolkit/utils'
import icon from '../../resources/icon.png?asset'
import type { IpcMainInvokeEvent, WebContents } from 'electron'
import { createRoom, issueInvitation, redeemInvitation, closeRoom, getRoom } from './rooms'
import type { RoomResult } from '../shared/auth'

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

function isTrustedRenderer(event: IpcMainInvokeEvent): boolean {
  const frame = event.senderFrame
  return frame !== null && frame === event.sender.mainFrame && frame.url === rendererUrl()
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

  // Другой запрос мог уже обновить пару, пока этот ждал свой 401.
  if (session.tokens === initialTokens) {
    if (session.refreshUncertain) {
      sessions.delete(sender)
      return { ok: false, error: 'sign_in_required' }
    }

    const pending = (session.refreshing ??= refreshSession(initialTokens))
    const refreshed = await pending

    if (sessions.get(sender) !== session) return { ok: false, error: 'unexpected' }

    if (session.refreshing === pending) {
      session.refreshing = undefined
      if (refreshed.ok) session.tokens = refreshed.tokens
      else if (refreshed.reason === 'uncertain') session.refreshUncertain = true
    }

    if (!refreshed.ok) {
      if (refreshed.reason === 'rate_limited') return { ok: false, error: 'rate_limited' }
      if (refreshed.reason === 'uncertain') return { ok: false, error: 'refresh_uncertain' }

      sessions.delete(sender)
      return { ok: false, error: 'sign_in_required' }
    }
  }

  const retryTokens = session.tokens
  const retry = await action(retryTokens.accessToken)

  if (sessions.get(sender) !== session) return { ok: false, error: 'unexpected' }
  if (!retry.ok && retry.error === 'sign_in_required' && session.tokens === retryTokens) {
    sessions.delete(sender)
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
      sessions.delete(sender)

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
    sessions.delete(sender)

    if (!session) return 'no_local_session'

    const refreshed = await session.refreshing
    const token = refreshed?.ok ? refreshed.tokens.accessToken : session.tokens.accessToken
    return revokeCurrentSession(token)
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
