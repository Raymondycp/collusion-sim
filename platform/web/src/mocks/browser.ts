import { setupWorker } from 'msw/browser'
import { handlers, wsHandlers } from './handlers'

export async function startMocks() {
  await setupWorker(...handlers, ...wsHandlers).start({ quiet: true, onUnhandledFrame: 'bypass' })
}
