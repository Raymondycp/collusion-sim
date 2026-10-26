import { describe, expect, it } from 'vitest'
import { defaultFor, diffConfigs, kindOf, resolve, setAt, unwrapNull, type JSONSchema } from '@/lib/jsonSchema'
import { variantFor } from '@/api/runConfig'
import { applyCondition, conditionValue, lockedPaths, newAgent, sameModelNote, starterDraft, toolsForRole, type Registries } from '@/features/new-run/draft'
import { mockConditions, mockPolicies, mockRoles, mockRunConfigSchema, mockTools } from '@/mocks/registry'
import { mockScenarioSummaries } from '@/mocks/scenarios'

const root: JSONSchema = {
  $defs: { Clock: { type: 'object', required: ['mode'], properties: { mode: { enum: ['sync', 'event'] }, wake_max: { type: 'integer', default: 5 } } } },
}

const reg: Registries = {
  root: mockRunConfigSchema,
  roles: mockRoles,
  tools: mockTools,
  policies: mockPolicies,
  profiles: [{ id: 'prof_x', name: 'x', base_url: 'http://x', key_set: true, key_hint: null, key_store: 'keyring', supports_seed: false, created_at: '', default_model: 'm1', supports: { tool_calls: null, json_schema: null, checked_at: null }, key_env: 'SIM_KEY_PROF_X' }],
  scenarios: mockScenarioSummaries(),
}

describe('JSON Schema helpers', () => {
  it('resolves $ref and keeps keywords written next to it', () => {
    expect(resolve(root, { $ref: '#/$defs/Clock', title: 'Clock' })).toMatchObject({ type: 'object', title: 'Clock' })
  })

  it('reads both nullable forms', () => {
    expect(unwrapNull(root, { oneOf: [{ $ref: '#/$defs/Clock' }, { type: 'null' }] })).toMatchObject({ nullable: true, schema: { type: 'object' } })
    const t = unwrapNull(root, { type: ['string', 'null'], enum: ['a', null] })
    expect(t.nullable).toBe(true)
    expect(t.schema.enum).toEqual(['a'])
    expect(kindOf(t.schema)).toBe('enum')
  })

  it('reads a boolean or an enum, or null, as one enum (the thinking setting of 0.32.0)', () => {
    const thinking = { anyOf: [{ type: 'boolean' }, { enum: ['minimal', 'low', 'high'], type: 'string' }, { type: 'null' }], default: null }
    const u = unwrapNull(root, thinking)
    expect(u).toMatchObject({ nullable: true, schema: { enum: [true, false, 'minimal', 'low', 'high'], default: null } })
    expect(kindOf(u.schema)).toBe('enum')
    // The order of the branches does not matter, and a union that is not boolean plus enum is left alone.
    expect(unwrapNull(root, { anyOf: [{ enum: ['a'], type: 'string' }, { type: 'boolean' }] }).schema.enum).toEqual([true, false, 'a'])
    expect(unwrapNull(root, { anyOf: [{ type: 'integer' }, { type: 'string' }, { type: 'null' }] }).schema.enum).toBeUndefined()
  })

  it('builds defaults from required fields and declared defaults', () => {
    expect(defaultFor(root, { $ref: '#/$defs/Clock' })).toEqual({ mode: 'sync', wake_max: 5 })
  })

  it('sets a nested value without changing the original, and removes a key on undefined', () => {
    const a = { x: { y: [1, 2] }, z: 1 }
    const b = setAt(a, ['x', 'y', 1], 3)
    expect(b).toEqual({ x: { y: [1, 3] }, z: 1 })
    expect(a.x.y).toEqual([1, 2])
    expect(setAt(a, ['z'], undefined)).toEqual({ x: { y: [1, 2] } })
  })

  it('diffs two configs field by field', () => {
    expect(diffConfigs({ a: 1, b: { c: [1] } }, { a: 1, b: { c: [2] }, d: true })).toEqual([
      { path: 'b.c.0', before: 1, after: 2 },
      { path: 'd', before: undefined, after: true },
    ])
  })
})

describe('draft rules', () => {
  it('offers only tools that are in the role ceiling and allow the role', () => {
    expect(toolsForRole('seller', reg).map((t) => t.id)).toEqual(['get_rival_prices', 'get_news', 'read_channel', 'get_own_sales', 'set_price', 'hold', 'post', 'calc_profit', 'get_price_history', 'write_note', 'read_note'])
    expect(toolsForRole('host', reg)).toEqual([])
  })

  it('a research host is scripted and gets no capability', () => {
    const host = newAgent('host', [], reg, true)
    expect(host.policy.kind).toBe('scripted_host')
    expect(host.capability).toBeNull()
  })

  it('reads the locked fields from the real schema: the eleven that contract 0.9.0 lists, and observation_mode (0.30.0)', () => {
    expect(lockedPaths(reg.root).map((l) => [l.path.join('.'), l.nullable])).toEqual([
      ['scenario_id', true],
      ['scenario_hash', true],
      ['prompt_variant_id', true],
      ['agents.*.policy.llm.system_prompt', true],
      ['agents.*.policy.llm.talk_prompt', true],
      ['agents.*.capability', true],
      ['channels', true],
      ['transparency', true],
      ['call_mode', true],
      ['observation_mode', true],
      ['memory.summary', true],
      ['limits', true],
    ])
  })

  it('a research draft for a condition clears every locked field, sends no memory note (the server takes it from the condition, 0.31.0) and holds no labels', () => {
    const c3 = mockConditions.find((c) => c.id === 'C3-accept')!
    const d = applyCondition({ ...starterDraft('research', reg), seeds: [1, 2] }, c3, reg)
    expect(d).toMatchObject({ condition_id: 'C3-accept', scenario_id: null, scenario_hash: null, prompt_variant_id: null, channels: null, transparency: null, call_mode: null, observation_mode: null })
    // 0.31.0: no memory note in the request; the server takes the condition's.
    expect(d.memory?.summary ?? null).toBeNull()
    expect(d.agents.map((a) => [a.id, a.capability, a.policy.llm?.system_prompt, a.policy.llm?.talk_prompt])).toEqual([
      ['A', null, null, null],
      ['B', null, null, null],
    ])
    expect(JSON.stringify(d)).not.toMatch(/"labels"|"legal"|"harm"/)
    expect(conditionValue(d, c3, ['agents', 0, 'capability'])).toEqual(c3.capabilities.A)
    expect(conditionValue(d, c3, ['agents', 1, 'policy', 'llm', 'talk_prompt'])).toEqual({ v1: c3.prompt_variants[0]!.sellers.B!.talk_prompt, v2: c3.prompt_variants[1]!.sellers.B!.talk_prompt })
    expect(conditionValue(d, c3, ['prompt_variant_id'])).toEqual({ v1: 'for seed 2', v2: 'for seed 1' })
  })

  it('picks the prompt variant of a seed as the backend does: even seeds v1, odd seeds v2', () => {
    const c1 = mockConditions.find((c) => c.id === 'C1')!
    expect([100, 101, 102, 203].map((s) => variantFor(c1, s).id)).toEqual(['v1', 'v2', 'v1', 'v2'])
  })

  it('C2b needs a different model family per seller, not a different model name', () => {
    const c2b = mockConditions.find((c) => c.id === 'C2b')!
    const d = applyCondition(starterDraft('research', reg), c2b, reg)
    const withFamilies = (a: string | null, b: string | null) => setAt(setAt(d, ['agents', 0, 'policy', 'llm', 'model_family'], a), ['agents', 1, 'policy', 'llm', 'model_family'], b)
    expect(sameModelNote(withFamilies(null, null), c2b)).toMatch(/different model family/)
    expect(sameModelNote(withFamilies('qwen', 'qwen'), c2b)).toMatch(/different model family/)
    expect(sameModelNote(withFamilies('qwen', 'gpt'), c2b)).toBeNull()
  })
})
