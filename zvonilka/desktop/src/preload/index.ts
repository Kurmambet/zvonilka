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
import type { AudioEvent } from '../shared/audio'

type IceResult = Awaited<ReturnType<typeof import('../main/rooms').getIceServers>>

const api: DesktopApi & { getIceServers: () => Promise<IceResult> } = {
  getIceServers: () => ipcRenderer.invoke('ice:get') as Promise<IceResult>,
  sendAudio: (peer, signal) => ipcRenderer.invoke('audio:send', peer, signal) as Promise<boolean>,
  onAudio: (listener) => {
    const handler = (_event: Electron.IpcRendererEvent, value: AudioEvent): void => listener(value)
    ipcRenderer.on('audio:event', handler)
    return () => {
      ipcRenderer.removeListener('audio:event', handler)
    }
  },
  login: (profileId, username, password) =>
    ipcRenderer.invoke('auth:login', profileId, username, password) as Promise<LoginResult>,

  probeServer: (id) =>
    ipcRenderer.invoke('server:probe', id) as ReturnType<DesktopApi['probeServer']>,

  lookupUser: (username) =>
    ipcRenderer.invoke('rooms:lookup-user', username) as ReturnType<DesktopApi['lookupUser']>,
  sendDirectInvite: (roomId, username) =>
    ipcRenderer.invoke('rooms:send-direct', roomId, username) as ReturnType<
      DesktopApi['sendDirectInvite']
    >,
  directInbox: () =>
    ipcRenderer.invoke('rooms:direct-inbox') as ReturnType<DesktopApi['directInbox']>,
  acceptDirectInvite: (roomId, id) =>
    ipcRenderer.invoke('rooms:accept-direct', roomId, id) as ReturnType<
      DesktopApi['acceptDirectInvite']
    >,
  declineDirectInvite: (roomId, id) =>
    ipcRenderer.invoke('rooms:decline-direct', roomId, id) as ReturnType<
      DesktopApi['declineDirectInvite']
    >,

  createRoom: () => ipcRenderer.invoke('rooms:create') as Promise<RoomResult<RoomSummary>>,

  issueInvitation: (roomId) =>
    ipcRenderer.invoke('rooms:issue-invitation', roomId) as Promise<RoomResult<string>>,

  redeemInvitation: (invitation) =>
    ipcRenderer.invoke('rooms:redeem-invitation', invitation) as Promise<RoomResult<RoomSummary>>,

  closeRoom: (roomId) => ipcRenderer.invoke('rooms:close', roomId) as Promise<RoomResult<null>>,

  register: (profileId, username, displayName, password) =>
    ipcRenderer.invoke(
      'auth:register',
      profileId,
      username,
      displayName,
      password
    ) as Promise<RegisterResult>,

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
