// Mock provider-profile endpoints. Like the real backend, the mock keeps only a hint and drops the key.
import { http, HttpResponse } from 'msw/http'
import type { ProviderProfile } from '@/api/providers'
import type { Problem } from '@/api/types'
import type { Req, Res } from './contract'

const problem = (status: number, code: Problem['code'], message: string, errors: Problem['errors'] = []) =>
  HttpResponse.json({ title: message, status, code, message, errors } satisfies Problem, { status, headers: { 'Content-Type': 'application/problem+json' } })

const hint = (key: string) => (key.length >= 20 ? `${key.slice(0, 3)}…${key.slice(-4)}` : '…')

/** Runs in data.ts use prof_mock, so deleting it gives profile_in_use. */
const IN_USE = new Set(['prof_mock'])

let profiles: ProviderProfile[] = []

/** SIM_KEY_<PROFILE_ID>: upper case, non-alphanumerics as _ (contract v0.5.1). */
export const keyEnv = (id: string) => `SIM_KEY_${id.toUpperCase().replace(/[^A-Z0-9]/g, '_')}`

/** The real server refuses paid calls unless it runs with SIM_ALLOW_PAID=1. The mock allows them unless a test turns them off. */
let paidAllowed = true
export const setMockPaidCalls = (allowed: boolean) => void (paidAllowed = allowed)
export const paidCallsOff = () =>
  paidAllowed ? null : problem(409, 'paid_calls_disabled', 'Paid LLM calls are disabled on this server. Start it with SIM_ALLOW_PAID=1 after lead approval.')

export function resetMockProfiles() {
  paidAllowed = true
  profiles = [
    {
      id: 'prof_mock',
      name: 'Mock endpoint',
      base_url: 'http://127.0.0.1:9/v1',
      key_set: true,
      key_hint: 'sk-…mock',
      key_store: 'keyring',
      default_model: 'mock-model',
      pricing: { input_usd_per_mtok: 0.15, output_usd_per_mtok: 0.6 },
      supports: { tool_calls: true, json_schema: true, checked_at: '2026-10-21T10:02:00Z' },
      max_concurrency: 2,
      supports_seed: false,
      created_at: '2026-10-21T10:00:00Z',
      key_env: keyEnv('prof_mock'),
      source: 'store',
      read_only: false,
      protocol: 'openai_chat',
      max_calls_per_5h: 300,
      rate_window: { max_calls_per_5h: 300, used: 12, room: 288, rate_limited: 0 },
      session_header: null,
    },
    {
      id: 'prof_env_local',
      name: 'Local vLLM (.env)',
      base_url: 'http://127.0.0.1:8001/v1',
      key_set: true,
      key_hint: 'tok…0000',
      key_store: 'env',
      default_model: null,
      pricing: null,
      supports: { tool_calls: null, json_schema: null, checked_at: null },
      max_concurrency: 1,
      supports_seed: true,
      created_at: '2026-10-21T10:05:00Z',
      key_env: 'SIM_PROFILE_ENV_LOCAL_KEY',
      source: 'env',
      read_only: true,
      protocol: 'openai_chat',
      max_calls_per_5h: null,
      rate_window: null,
      session_header: 'x-opencode-session',
    },
  ]
}
resetMockProfiles()

const readOnly = (id: string) => problem(409, 'profile_read_only', `Profile ${id} comes from .env and is read-only here. Change it in .env, then restart the server.`)

const slug = (name: string) => `prof_${name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '') || 'x'}`

export const providerHandlers = [
  http.get('/api/provider-profiles', () => HttpResponse.json(profiles)),

  http.post<never, Req<'/provider-profiles', 'post'>>('/api/provider-profiles', async ({ request }) => {
    const b = await request.json()
    if (!b.name || !b.base_url || !b.api_key) return problem(422, 'validation_error', 'Name, base URL and key are required.')
    if (b.protocol === 'typesafe_systemone') return problem(422, 'protocol_not_built', 'Protocol typesafe_systemone is registered but not built.', [{ path: 'protocol', message: 'not built' }])
    let id = slug(b.name)
    while (profiles.some((p) => p.id === id)) id += '_2'
    const p: ProviderProfile = {
      id,
      name: b.name,
      base_url: b.base_url,
      key_set: true,
      key_hint: hint(b.api_key),
      key_store: 'keyring',
      default_model: b.default_model ?? null,
      pricing: b.pricing ?? null,
      supports: { tool_calls: null, json_schema: null, checked_at: null },
      max_concurrency: b.max_concurrency ?? 2,
      supports_seed: b.supports_seed ?? false,
      created_at: new Date().toISOString(),
      key_env: keyEnv(id),
      source: 'store',
      read_only: false,
      protocol: b.protocol ?? 'openai_chat',
      max_calls_per_5h: b.max_calls_per_5h ?? null,
      rate_window: b.max_calls_per_5h ? { max_calls_per_5h: b.max_calls_per_5h, used: 0, room: b.max_calls_per_5h, rate_limited: 0 } : null,
      session_header: b.session_header ?? null,
    }
    profiles = [...profiles, p]
    return HttpResponse.json(p, { status: 201 })
  }),

  http.get('/api/provider-profiles/:id', ({ params }) => {
    const p = profiles.find((x) => x.id === params.id)
    return p ? HttpResponse.json(p) : problem(404, 'not_found', `Profile ${params.id} was not found.`)
  }),

  http.patch<{ id: string }, Req<'/provider-profiles/{profile_id}', 'patch'>>('/api/provider-profiles/:id', async ({ params, request }) => {
    const p = profiles.find((x) => x.id === params.id)
    if (!p) return problem(404, 'not_found', `Profile ${params.id} was not found.`)
    if (p.read_only) return readOnly(p.id)
    const { api_key, ...rest } = await request.json()
    const next: ProviderProfile = { ...p, ...rest, ...(api_key ? { key_set: true, key_hint: hint(api_key), key_store: 'keyring' as const } : {}) }
    profiles = profiles.map((x) => (x.id === p.id ? next : x))
    return HttpResponse.json(next)
  }),

  http.delete('/api/provider-profiles/:id', ({ params }) => {
    const id = String(params.id)
    if (profiles.find((x) => x.id === id)?.read_only) return readOnly(id)
    if (IN_USE.has(id)) return problem(409, 'profile_in_use', `Profile ${id} is used by a queued or running run. Stop the run first.`)
    profiles = profiles.filter((x) => x.id !== id)
    return new HttpResponse(null, { status: 204 })
  }),

  http.post('/api/provider-profiles/:id/test', async ({ params }) => {
    const p = profiles.find((x) => x.id === params.id)
    if (!p) return problem(404, 'not_found', `Profile ${params.id} was not found.`)
    const off = paidCallsOff()
    if (off) return off
    const model = p.default_model ?? 'unknown'
    if (p.key_store === 'env') {
      return HttpResponse.json({ ok: false, model, latency_ms: null, supports: p.supports, error: { kind: 'network', http_status: null, message: 'The endpoint did not answer (mock).' } } satisfies Res<'/provider-profiles/{profile_id}/test', 'post'>)
    }
    const supports = { tool_calls: true, json_schema: true, checked_at: new Date().toISOString() }
    profiles = profiles.map((x) => (x.id === p.id ? { ...x, supports } : x))
    return HttpResponse.json({ ok: true, model, latency_ms: 412, supports, error: null } satisfies Res<'/provider-profiles/{profile_id}/test', 'post'>)
  }),

  http.get('/api/provider-profiles/:id/models', ({ params }) =>
    HttpResponse.json((params.id === 'prof_env_local' ? { models: [], available: false } : { models: ['mock-model', 'mock-model-large'], available: true }) satisfies Res<'/provider-profiles/{profile_id}/models', 'get'>),
  ),
]
