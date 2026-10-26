import createClient from 'openapi-fetch'
import type { paths } from './schema'
import type { Problem } from './types'

export const API_BASE = '/api'

// Absolute base and a late-bound fetch, so Node tests can intercept requests made by this client.
export const api = createClient<paths>({ baseUrl: `${location.origin}${API_BASE}`, fetch: (r) => globalThis.fetch(r) })

export class ApiError extends Error {
  readonly status: number
  /** `http`: the response was not a Problem (for example a path this backend does not serve). */
  readonly code: Problem['code'] | 'network' | 'http'
  /** Field errors from a 422, by dot path (for example `agents.0.capability`). */
  readonly fields: { path: string; message: string }[]
  constructor(status: number, code: ApiError['code'], message: string, fields: ApiError['fields'] = []) {
    super(message)
    this.status = status
    this.code = code
    this.fields = fields
  }
}

/** For a catch block: an ApiError as it is, anything else as a generic error that the page can show. The original goes to the console. */
export function asApiError(e: unknown): ApiError {
  if (e instanceof ApiError) return e
  console.error(e)
  return new ApiError(0, 'http', 'Unexpected error: see the browser console.')
}

/** Codes whose UI text is fixed, whatever the server message says. */
const PROBLEM_TEXT: Partial<Record<Problem['code'], string>> = {
  paid_calls_disabled: 'Paid calls are off on this server. The lead can turn them on (SIM_ALLOW_PAID=1).',
}

function isProblem(x: unknown): x is Problem {
  return typeof x === 'object' && x !== null && 'code' in x && 'message' in x
}

/** Unwraps an openapi-fetch result. Shows only the server's short message, never a request body. */
export async function unwrap<T>(p: Promise<{ data?: T; error?: unknown; response: Response }>): Promise<T> {
  let r: Awaited<typeof p>
  try {
    r = await p
  } catch {
    throw new ApiError(0, 'network', 'The backend did not answer. Is it running on 127.0.0.1:8000?')
  }
  if (r.error !== undefined || !r.response.ok) {
    if (isProblem(r.error)) {
      // The backend writes field paths with slashes (`agents/0/role`); the forms key their fields with dots.
      const fields = (r.error.errors ?? []).map((f) => ({ ...f, path: f.path.replaceAll('/', '.') }))
      throw new ApiError(r.response.status, r.error.code, PROBLEM_TEXT[r.error.code] ?? r.error.message, fields)
    }
    const path = new URL(r.response.url || location.href).pathname
    throw new ApiError(
      r.response.status,
      'http',
      r.response.status === 404 ? `This backend does not serve ${path} yet (HTTP 404).` : `Request to ${path} failed (HTTP ${r.response.status}).`,
    )
  }
  return r.data as T
}

/** The route is in the contract but the server does not build it (501 `not_built`). */
export const isNotBuilt = (e: unknown) => e instanceof ApiError && e.code === 'not_built'

/** One retry for a lost connection or a server fault. A 4xx answer or 501 `not_built` is final: a retry gets the same answer. */
export function retryOnce(failures: number, error: unknown): boolean {
  if (failures >= 1) return false
  if (!(error instanceof ApiError)) return true
  return error.code === 'network' || (error.status >= 500 && error.code !== 'not_built')
}

export function wsUrl(path: string): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${location.host}${API_BASE}${path}`
}
