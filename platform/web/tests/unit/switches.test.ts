import { describe, expect, it } from 'vitest'
import type { AgentConfig, RunConfig } from '@/api/runConfig'
import { addAgent, OPTIONAL_TOOLS, pairId, pullBlind, pullWithoutReads, removeAgent, switched, type Registries } from '@/features/new-run/draft'
import { mockTools } from '@/mocks/registry'
import { realRunConfigSchema } from '@/mocks/researchFixtures'

const tool = (id: string) => mockTools.find((t) => t.id === id)!
const RECORD_READS = mockTools.filter((t) => t.roles.includes('seller') && t.counts_as_query).map((t) => t.id)

type Combo = { news: boolean; opaque: boolean; mode: 'push' | 'pull' | 'both' | null; pub: boolean; pairs: boolean }
const combos: Combo[] = []
for (const news of [true, false])
  for (const opaque of [true, false])
    for (const mode of ['push', 'pull', 'both', null] as const)
      for (const pub of [true, false]) for (const pairs of [true, false]) combos.push({ news, opaque, mode, pub, pairs })

const request = (c: Combo) =>
  ({ channels: { public: c.pub, private_pairs: c.pairs, news: c.news }, transparency: c.opaque ? 'opaque' : 'rival_last_round', observation_mode: c.mode }) as unknown as RunConfig

describe('run switches over grants: every combination (0.27.0, 0.30.0)', () => {
  it('covers 64 combinations of news, transparency, observation mode and the two channel switches', () => {
    expect(combos).toHaveLength(64)
  })

  it.each(combos)('%o', (c) => {
    const r = request(c)
    const at = (id: string, channels?: string[]) => switched(tool(id), { tool_id: id, params: channels ? { channels } : {} }, r)
    // Offered is not effective (CR-83 1). In push a record read is not offered, but its grant still gates the pushed item, so
    // it stays on with a note. A tool is off by its own switch, and get_price_history is off in push (it has no pushed field).
    for (const id of RECORD_READS.filter((x) => x !== 'read_channel')) {
      const own = (id === 'get_news' && !c.news) || (id === 'get_rival_prices' && c.opaque)
      const histPush = id === 'get_price_history' && c.mode === 'push'
      expect(!!at(id).off, id).toBe(own || histPush)
      expect(at(id).note, id).toBe(c.mode === 'push' && !own && !histPush ? 'Push shows this item in every call. The agent has no read tool for it.' : null)
    }
    for (const id of ['set_price', 'hold', 'calc_profit', 'write_note', 'read_note']) expect(at(id), id).toEqual({ off: null, note: null })
    // read_channel and post on public and one pair: off when both are closed, a note when one is (the push note when none is).
    const both = ['public', 'private_A_B']
    const closed = [c.pub ? null : 'public', c.pairs ? null : 'private_A_B'].filter(Boolean)
    for (const id of ['read_channel', 'post']) {
      const s = at(id, both)
      expect(!!s.off, id).toBe(closed.length === 2)
      const pushNote = id === 'read_channel' && c.mode === 'push' ? 'Push shows this item in every call. The agent has no read tool for it.' : null
      expect(s.note, id).toBe(closed.length === 1 ? `The run does not open ${closed[0]}, so the server takes it out of this grant.` : closed.length === 0 ? pushNote : null)
    }
    // A grant with no channel is dropped by the server whatever the switches say: a note, so its channels stay editable.
    expect(at('post', [])).toEqual({ off: null, note: 'The grant names no channel, so the server drops it. Add a channel below.' })
  })
})

describe('pull needs read calls (0.30.0)', () => {
  const seller = (q: number | undefined) => ({ id: 'A', role: 'seller', policy: { kind: 'constant', params: {} }, capability: { grants: [], max_queries_per_round: q } }) as unknown as AgentConfig
  const req = (mode: string | null, limit?: number) => ({ observation_mode: mode, limits: limit === undefined ? null : { max_queries_per_round: limit } }) as unknown as RunConfig
  it.each([
    ['pull', 0, 3, true],
    ['pull', 3, 0, true],
    ['pull', undefined, 0, true],
    ['pull', 3, 3, false],
    ['pull', undefined, undefined, false],
    ['push', 0, 0, false],
    ['both', 0, 0, false],
    [null, 0, 0, false],
  ] as const)('mode %s, limit %s, grant %s: refused %s', (mode, limit, grant, refused) => {
    expect(!!pullWithoutReads(seller(grant), req(mode, limit))).toBe(refused)
  })
})

describe('pull with no read tool (CR-83 4)', () => {
  const grants = (...ids: string[]) => ids.map((tool_id) => ({ tool_id, params: {} }))
  const seller = (ids: string[], q = 3) => ({ id: 'A', role: 'seller', policy: { kind: 'constant', params: {} }, capability: { grants: grants(...ids), max_queries_per_round: q } }) as unknown as AgentConfig
  const req = (over: Record<string, unknown>) => ({ observation_mode: 'pull', limits: null, ...over }) as unknown as RunConfig
  const blind = (a: AgentConfig, r: RunConfig) => pullBlind(a, r, mockTools)
  it('no read tool granted: a note, and the run is not refused', () => {
    expect(blind(seller(['set_price', 'hold', 'post', 'calc_profit']), req({}))).toMatch(/no read tool is granted/)
    expect(pullWithoutReads(seller(['set_price']), req({}))).toBeNull()
  })
  it('one read tool is enough, and an off read tool does not count', () => {
    expect(blind(seller(['set_price', 'get_own_sales']), req({}))).toBeNull()
    expect(blind(seller(['set_price', 'get_news']), req({ channels: { news: false } }))).toMatch(/no read tool is granted/)
    expect(blind(seller(['set_price', 'get_rival_prices']), req({ transparency: 'opaque' }))).toMatch(/no read tool is granted/)
    expect(blind(seller(['set_price', 'get_price_history']), req({}))).toBeNull()
  })
  it('not in another mode, and not when the server already refuses the run', () => {
    expect(blind(seller(['set_price']), req({ observation_mode: 'both' }))).toBeNull()
    expect(blind(seller(['set_price']), req({ observation_mode: 'push' }))).toBeNull()
    expect(blind(seller(['set_price'], 0), req({}))).toBeNull()
  })
})

describe('starter grants and channels (0.30.0, CR-69 addendum 3)', () => {
  const reg = {
    root: realRunConfigSchema,
    roles: [{ name: 'seller', tools: mockTools.filter((t) => t.roles.includes('seller')).map((t) => t.id) }, { name: 'host', tools: [] }],
    tools: mockTools,
    policies: [{ kind: 'constant', roles: ['seller', 'host'], params: [] }],
    profiles: [],
    scenarios: [],
  } as unknown as Registries
  const channels = (a: AgentConfig, id: string) => a.capability?.grants.find((g) => g.tool_id === id)?.params?.channels

  it('a new seller gets no optional tool, and names public and its pairs in read_channel and post', () => {
    const agents = addAgent('seller', addAgent('seller', [], reg), reg)
    const ids = agents[0]!.capability!.grants.map((g) => g.tool_id)
    expect(ids.filter((x) => OPTIONAL_TOOLS.has(x))).toEqual([])
    expect(ids).toEqual(expect.arrayContaining(['read_channel', 'post', 'set_price']))
    for (const a of agents) for (const id of ['read_channel', 'post']) expect(channels(a, id)).toEqual(['public', pairId('A', 'B')])
  })

  it('a third seller adds its pair to the others; removing it takes the pair out again', () => {
    const three = addAgent('seller', addAgent('seller', addAgent('seller', [], reg), reg), reg)
    expect(three.map((a) => channels(a, 'post'))).toEqual([
      ['public', 'private_A_B', 'private_A_C'],
      ['public', 'private_A_B', 'private_B_C'],
      ['public', 'private_A_C', 'private_B_C'],
    ])
    const two = removeAgent(three, 2)
    expect(two.map((a) => channels(a, 'read_channel'))).toEqual([
      ['public', 'private_A_B'],
      ['public', 'private_A_B'],
    ])
  })

  it('a seller grant that the user narrowed does not get the new pair (CR-83 3)', () => {
    const two = addAgent('seller', addAgent('seller', [], reg), reg)
    const narrow = two.map((a, i) => (i === 0 ? { ...a, capability: { ...a.capability!, grants: a.capability!.grants.map((g) => (g.tool_id === 'post' ? { ...g, params: { ...g.params, channels: ['public'] } } : g)) } } : a))
    const three = addAgent('seller', narrow, reg)
    expect(channels(three[0]!, 'post')).toEqual(['public'])
    expect(channels(three[0]!, 'read_channel')).toEqual(['public', 'private_A_B', 'private_A_C'])
    expect(channels(three[2]!, 'post')).toEqual(['public', 'private_A_C', 'private_B_C'])
  })

  it('a host keeps no channels and changes no seller', () => {
    const sellers = addAgent('seller', addAgent('seller', [], reg), reg)
    const withHost = addAgent('host', sellers, reg)
    expect(withHost.slice(0, 2)).toEqual(sellers)
  })
})
