import type { SocketPhase } from '@/api/socket'

/** The connection state of a run stream, as the workbench header shows it. */
export const PHASE_TEXT: Record<SocketPhase, string> = {
  connecting: 'Connecting',
  replaying: 'Loading the record',
  live: 'Live',
  ended: 'Replay',
  reconnecting: 'Connection lost. Reconnecting',
  not_found: 'Run not found',
  error: 'Stream error',
}
