import type { DesktopApi } from '../shared/auth'

declare global {
  interface Window {
    api: DesktopApi
  }
}
