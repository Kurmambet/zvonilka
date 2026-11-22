export type ServerProfileId = 'local' | 'staging'

export type ServerProbeResult = { ok: true; latencyMs: number } | { ok: false }
