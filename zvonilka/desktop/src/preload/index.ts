import { contextBridge, ipcRenderer } from 'electron'
import type {
  DesktopApi,
  LoginResult,
  RoomResult,
  RoomSummary,
  RegisterResult,
  LogoutResult
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
  logout: () => ipcRenderer.invoke('auth:logout') as Promise<LogoutResult>
}

contextBridge.exposeInMainWorld('api', api)
