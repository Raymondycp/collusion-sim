import { timingSafeEqual } from 'node:crypto'
import type { IncomingMessage } from 'node:http'
import type { Duplex } from 'node:stream'
import path from 'node:path'
import { loadEnv, type Plugin } from 'vite'

// One password at the door of the hosted site (user, 24 Oct): HTTP Basic auth on every page, /api request and WebSocket.
// SITE_PASSWORD comes from the process environment, else from the repo-root .env (only SITE_* names are read there).
// Off when it is unset, so local runs need nothing. Any user name is accepted; only the password is checked.
const fromEnvFile = () => loadEnv('', path.resolve(import.meta.dirname, '..'), 'SITE_').SITE_PASSWORD
function allowed(req: IncomingMessage, password: string): boolean {
  const header = req.headers.authorization ?? ''
  if (!header.startsWith('Basic ')) return false
  const decoded = Buffer.from(header.slice(6), 'base64').toString('utf8')
  const given = Buffer.from(decoded.slice(decoded.indexOf(':') + 1))
  const want = Buffer.from(password)
  return given.length === want.length && timingSafeEqual(given, want)
}

const CHALLENGE = { 'WWW-Authenticate': 'Basic realm="Market simulator", charset="UTF-8"' }

export function siteGate(password = process.env.SITE_PASSWORD || fromEnvFile()): Plugin {
  return {
    name: 'site-gate',
    configureServer(server) {
      if (!password) return
      server.middlewares.use((req, res, next) => {
        if (allowed(req, password)) return next()
        res.writeHead(401, { ...CHALLENGE, 'Content-Type': 'text/plain' }).end('Password required.')
      })
      server.httpServer?.prependListener('upgrade', (req: IncomingMessage, socket: Duplex) => {
        if (allowed(req, password)) return
        socket.end('HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: Basic realm="Market simulator"\r\n\r\n')
        socket.destroy()
      })
    },
  }
}
