import { describe, expect, it } from 'vitest'
import { ApiError, retryOnce, unwrap } from '@/api/client'

describe('query retry', () => {
  it.each([
    ['501 not_built', new ApiError(501, 'not_built', 'not built'), false],
    ['404 not_found', new ApiError(404, 'not_found', 'no run'), false],
    ['409 run_not_active', new ApiError(409, 'run_not_active', 'ended'), false],
    ['502 from the proxy', new ApiError(502, 'http', 'bad gateway'), true],
    ['no answer', new ApiError(0, 'network', 'down'), true],
    ['a thrown TypeError', new TypeError('x'), true],
  ] as const)('%s: retry once = %s', (_, error, retry) => {
    expect(retryOnce(0, error)).toBe(retry)
    expect(retryOnce(1, error)).toBe(false)
  })
})

describe('field errors', () => {
  it('turns the backend slash paths into the dot paths that the forms use', async () => {
    const answer = { error: { code: 'validation_error', message: 'bad', errors: [{ path: 'agents/0/role', message: 'Field required' }, { path: 'name', message: 'x' }] }, response: new Response(null, { status: 422 }) }
    const e = await unwrap(Promise.resolve(answer)).catch((x: unknown) => x)
    expect(e).toBeInstanceOf(ApiError)
    expect((e as ApiError).fields.map((f) => f.path)).toEqual(['agents.0.role', 'name'])
  })
})
