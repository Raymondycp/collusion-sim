import { bypass } from 'msw'
import { setupWorker } from 'msw/browser'
import { snapshotHandlers, type LoadFile, type SnapshotIndex } from './snapshot'

/** A snapshot file as JSON. A gzip file is read as gzip whatever headers the static server sends. */
const loadFile: LoadFile = async <T,>(name: string) => {
  const res = await fetch(bypass(`${import.meta.env.BASE_URL}snapshot/${name}`))
  if (!res.ok) throw new Error(`snapshot/${name}: ${res.status}`)
  const bytes = new Uint8Array(await res.arrayBuffer())
  const gzip = bytes[0] === 0x1f && bytes[1] === 0x8b
  const text = gzip ? await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'))).text() : new TextDecoder().decode(bytes)
  return JSON.parse(text) as T
}

export async function startSnapshot() {
  const index = await loadFile<SnapshotIndex>('index.json')
  await setupWorker(...snapshotHandlers(index, loadFile)).start({ quiet: true, onUnhandledFrame: 'bypass', serviceWorker: { url: `${import.meta.env.BASE_URL}mockServiceWorker.js` } })
}
