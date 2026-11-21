import { contextBridge, ipcRenderer } from 'electron'
import type {
  DesktopApi,
  LoginResult,
  RoomResult,
  RoomSummary,
  RegisterResult,
  LogoutResult,
  PresenceState
} from '../shared/auth'

const api: DesktopApi = {
  login: (username, password) =>
    ipcRenderer.invoke('auth:login', username, password) as Promise<LoginResult>,

  createRoom: () => ipcRenderer.invoke('rooms:create') as Promise<RoomResult<RoomSummary>>,

  issueInvitation: (roomId) =>
    ipcRenderer.invoke('rooms:issue-invitation', roomId) as Promise<RoomResult<string>>,

  redeemInvitation: (invitation) =>
    ipcRenderer.invoke('rooms:redeem-invitation', invitation) as Promise<RoomResult<RoomSummary>>,

  closeRoom: (roomId) => ipcRenderer.invoke('rooms:close', roomId) as Promise<RoomResult<null>>,

  register: (username, displayName, password) =>
    ipcRenderer.invoke('auth:register', username, displayName, password) as Promise<RegisterResult>,

  logout: () => ipcRenderer.invoke('auth:logout') as Promise<LogoutResult>,

  getRoom: (roomId) => ipcRenderer.invoke('rooms:get', roomId) as Promise<RoomResult<RoomSummary>>,

  connectRoom: (roomId) =>
    ipcRenderer.invoke('presence:connect', roomId) as Promise<RoomResult<null>>,

  disconnectRoom: () => ipcRenderer.invoke('presence:disconnect') as Promise<void>,

  onPresence: (listener) => {
    const handler = (_event: Electron.IpcRendererEvent, state: PresenceState): void => {
      listener(state)
    }
    ipcRenderer.on('presence:state', handler)
    return () => {
      ipcRenderer.removeListener('presence:state', handler)
    }
  }
}

contextBridge.exposeInMainWorld('api', api)
