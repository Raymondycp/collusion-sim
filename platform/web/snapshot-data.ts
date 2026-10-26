import fs from 'node:fs'
import path from 'node:path'
import type { Plugin } from 'vite'

// The recorded API answers (scripts/record-snapshot.mjs) live in web/snapshot/, outside public/, so that
// the normal build does not carry them. This plugin serves them in `vite --mode snapshot` and copies them,
// with the small static server serve.py, into the snapshot build.
const DATA = path.resolve(import.meta.dirname, 'snapshot')
const SERVE = path.resolve(import.meta.dirname, 'scripts', 'serve-snapshot.py')

export function snapshotData(): Plugin {
  let outDir = ''
  return {
    name: 'snapshot-data',
    configResolved(config) {
      outDir = path.resolve(config.root, config.build.outDir)
    },
    buildStart() {
      if (!fs.existsSync(path.join(DATA, 'index.json'))) this.error(`No snapshot in ${DATA}. Run node scripts/record-snapshot.mjs first.`)
    },
    configureServer(server) {
      server.middlewares.use('/snapshot', (req, res, next) => {
        const file = path.join(DATA, decodeURIComponent(new URL(req.url ?? '/', 'http://x').pathname))
        if (!file.startsWith(DATA + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return next()
        res.writeHead(200, { 'Content-Type': 'application/octet-stream', 'Cache-Control': 'no-cache' })
        fs.createReadStream(file).pipe(res)
      })
    },
    closeBundle() {
      fs.cpSync(DATA, path.join(outDir, 'snapshot'), { recursive: true })
      fs.copyFileSync(SERVE, path.join(outDir, 'serve.py'))
    },
  }
}
