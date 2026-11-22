import type { DesktopApi } from '../shared/auth'

declare global {
  interface Window {
    api: DesktopApi & {
      getIceServers: () => ReturnType<typeof import('../main/rooms').getIceServers>
    }
  }
}
