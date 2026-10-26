import { devRefusal, variantFor, variantSeedRule, type AgentConfig, type Condition, type Defaults, type PolicyType, type RoleSpec, type RunConfig, type ScenarioSummary, type ToolGrant, type ToolSpec } from '@/api/runConfig'
import type { ProviderProfile } from '@/api/providers'
import { defaultFor, getAt, resolve, setAt, unwrapNull, type JSONSchema, type Path } from '@/lib/jsonSchema'
import { DEFAULT_QUERIES } from '@/features/conditions/limits'

export type Registries = {
  root: JSONSchema
  roles: RoleSpec[]
  tools: ToolSpec[]
  policies: PolicyType[]
  profiles: ProviderProfile[]
  scenarios: ScenarioSummary[]
  /** Pre-fill for exploratory agents only; research ignores it (DECISIONS-9 §16). */
  defaults?: Defaults
}

/** Tools an agent of this role may be granted: in the role ceiling and allowing the role. */
export function toolsForRole(role: string, r: Pick<Registries, 'roles' | 'tools'>): ToolSpec[] {
  const ceiling = r.roles.find((x) => x.name === role)?.tools ?? []
  return r.tools.filter((t) => ceiling.includes(t.id) && t.roles.includes(role))
}

/** Optional seller tools (DECISIONS-15 §3.3): off unless granted, so a new agent does not get them. */
export const OPTIONAL_TOOLS = new Set(['calc_profit', 'get_price_history', 'write_note', 'read_note'])

/** Grants whose params name channels. A run switch takes a closed channel out of them (services/configs.CHANNEL_GRANTS). */
const CHANNEL_GRANTS = new Set(['read_channel', 'post'])

const isOpen = (channelId: string, request: RunConfig | undefined) => (channelId === 'public' ? request?.channels?.public !== false : request?.channels?.private_pairs !== false)

/** What a run switch does to one grant: `off` when the grant does not work in this run, `note` when the server trims it. */
export type Switched = { off: string | null; note: string | null }

/**
 * The run switches win over the grants (services/configs.switched; DECISIONS-15 §3.1, CR-69). News off drops get_news,
 * opaque drops get_rival_prices, and a closed channel leaves read_channel and post, which the server drops when no channel
 * is left. The grant stays in the request, so it comes back when the switch is on again.
 *
 * Offered and effective are two things (CR-83 1). In observation_mode push no read tool is offered, but the grant still
 * decides what the call is given: news needs get_news, rival prices need get_rival_prices, own rounds need get_own_sales
 * and messages need read_channel. So these grants stay on and editable, with a note. Only get_price_history has no
 * pushed field, so push makes that grant useless.
 */
export function switched(tool: Pick<ToolSpec, 'id' | 'counts_as_query'>, grant: ToolGrant | undefined, request: RunConfig | undefined): Switched {
  if (tool.id === 'get_news' && request?.channels?.news === false) return { off: 'Off: the run has no news (Market rules, channels).', note: null }
  if (tool.id === 'get_rival_prices' && request?.transparency === 'opaque') return { off: 'Off: the run is opaque (Market rules, transparency).', note: null }
  const push = request?.observation_mode === 'push'
  if (push && tool.id === 'get_price_history') return { off: 'Off: observation mode push gives no read, and this tool has no pushed field (Market rules).', note: null }
  const pushed = push && tool.counts_as_query ? PUSH_NOTE : null
  if (CHANNEL_GRANTS.has(tool.id) && grant) {
    const named = Array.isArray(grant.params?.channels) ? (grant.params.channels as string[]) : []
    const closed = named.filter((c) => !isOpen(c, request))
    if (!named.length) return { off: null, note: 'The grant names no channel, so the server drops it. Add a channel below.' }
    if (closed.length === named.length) return { off: `Off: the run does not open ${closed.join(', ')} (Market rules, channels).`, note: null }
    if (closed.length) return { off: null, note: `The run does not open ${closed.join(', ')}, so the server takes ${closed.length > 1 ? 'them' : 'it'} out of this grant.` }
  }
  return { off: null, note: pushed }
}

const PUSH_NOTE = 'Push shows this item in every call. The agent has no read tool for it.'

/** observation_mode pull, read calls allowed, but no read tool granted: the agent sees nothing. The server does not refuse this run. */
export function pullBlind(agent: AgentConfig, request: RunConfig | undefined, tools: ToolSpec[]): string | null {
  if (request?.observation_mode !== 'pull' || agent.role !== 'seller' || !agent.capability || pullWithoutReads(agent, request)) return null
  const grants = agent.capability.grants
  const reads = tools.filter((t) => t.counts_as_query && grants.some((g) => g.tool_id === t.id) && !switched(t, grants.find((g) => g.tool_id === t.id), request).off)
  return reads.length ? null : `Observation mode pull gives ${agent.id} only the round and its last result, and no read tool is granted. ${agent.id} sees nothing. The server does not refuse this run.`
}

/** observation_mode pull with no read calls: the server refuses the run (simcore/run.py; 0.30.0). */
export function pullWithoutReads(agent: AgentConfig, request: RunConfig | undefined): string | null {
  if (request?.observation_mode !== 'pull' || agent.role !== 'seller' || !agent.capability) return null
  const run = request.limits?.max_queries_per_round ?? DEFAULT_QUERIES
  const own = agent.capability.max_queries_per_round ?? DEFAULT_QUERIES
  return Math.min(run, own) === 0 ? `Observation mode pull needs read calls, but ${agent.id} has none (limit ${run}, tool grant ${own}). The server refuses this run.` : null
}

export function defaultGrant(t: ToolSpec, root: JSONSchema): ToolGrant {
  return { tool_id: t.id, params: (defaultFor(root, t.params_schema as JSONSchema) as Record<string, unknown>) ?? {} }
}

export function policyParams(p: PolicyType | undefined): Record<string, unknown> {
  return Object.fromEntries((p?.params ?? []).filter((x) => x.default !== undefined).map((x) => [x.name, x.default]))
}

/** LLM settings for a new agent. With `agentId`, the server's SIM_DEFAULT_SELLER[_<ID>] pre-fill applies (exploratory only). */
export function defaultLlm(r: Registries, agentId?: string) {
  const def = r.root.$defs?.LlmSettings
  const base = (def ? defaultFor(r.root, def) : {}) as Record<string, unknown>
  const d = agentId === undefined ? undefined : r.defaults?.seller
  const own = d?.agents[agentId ?? '']
  const named = own?.profile_id ?? d?.profile_id
  const found = named ? r.profiles.find((p) => p.id === named) : undefined
  const profile = found ?? r.profiles.find((p) => p.key_set)
  // CR-24 m3: a default model belongs to its default profile, so a missing profile drops the model too.
  const defaultModel = named && !found ? null : (own?.model ?? d?.model)
  return { ...base, provider_profile_id: profile?.id ?? '', model: defaultModel ?? profile?.default_model ?? '' }
}

/** The profile that GET /defaults names for this exploratory agent, when the server has no such profile. */
export function missingDefaultProfile(r: Registries, agentId: string): string | null {
  const d = r.defaults?.seller
  const named = d?.agents[agentId]?.profile_id ?? d?.profile_id
  return named && !r.profiles.some((p) => p.id === named) ? named : null
}

/** Policies that may fill a role. A research run gives a scripted-only role no LLM policy. */
export function policiesForRole(role: string, research: boolean, r: Pick<Registries, 'roles' | 'policies'>): PolicyType[] {
  const scriptedOnly = research && r.roles.find((x) => x.name === role)?.research_scripted_only
  return r.policies.filter((p) => p.roles.includes(role) && !(scriptedOnly && p.kind === 'llm'))
}

/** The channels a seller can use: public and its private pair with each other seller (simcore/run.py channel ids). */
export const pairId = (a: string, b: string) => `private_${[a, b].sort().join('_')}`

/**
 * A new seller's channel grants name public and its pairs. The other sellers get the new pair only where the grant
 * holds the default, all its channels (CR-83 3). A grant that the user narrowed, for example to public, stays as it is.
 */
function withChannels(agent: AgentConfig, agents: AgentConfig[]): AgentConfig[] {
  const sellers = agents.filter((a) => a.role === 'seller').map((a) => a.id)
  const grantWith = (a: AgentConfig, change: (named: string[]) => string[] | null): AgentConfig =>
    a.capability
      ? { ...a, capability: { ...a.capability, grants: a.capability.grants.map((g) => (CHANNEL_GRANTS.has(g.tool_id) ? { ...g, params: { ...g.params, channels: change((g.params?.channels as string[] | undefined) ?? []) ?? g.params?.channels } } : g)) } }
      : a
  const holdsAll = (a: AgentConfig) => (named: string[]) => {
    const all = ['public', ...sellers.filter((o) => o !== a.id).map((o) => pairId(a.id, o))]
    return all.length === named.length && all.every((c) => named.includes(c)) ? [...named, pairId(a.id, agent.id)] : null
  }
  return [...agents.map((a) => (a.role === 'seller' ? grantWith(a, holdsAll(a)) : a)), grantWith(agent, () => ['public', ...sellers.map((o) => pairId(agent.id, o))])]
}

/** The agents with one more agent of this role. A seller gets its channels, and the other sellers get its pair. */
export function addAgent(role: string, agents: AgentConfig[], r: Registries, research = false): AgentConfig[] {
  const agent = newAgent(role, agents, r, research)
  return role === 'seller' ? withChannels(agent, agents) : [...agents, agent]
}

/** The agents without agent i. A removed seller's pairs leave the other sellers' channel grants. */
export function removeAgent(agents: AgentConfig[], i: number): AgentConfig[] {
  const gone = agents[i]
  const rest = agents.filter((_, j) => j !== i)
  if (gone?.role !== 'seller') return rest
  const pairs = new Set(rest.map((a) => pairId(a.id, gone.id)))
  return rest.map((a) =>
    a.capability
      ? { ...a, capability: { ...a.capability, grants: a.capability.grants.map((g) => (CHANNEL_GRANTS.has(g.tool_id) && Array.isArray(g.params?.channels) ? { ...g, params: { ...g.params, channels: (g.params.channels as string[]).filter((c) => !pairs.has(c)) } } : g)) } }
      : a,
  )
}

export function nextSellerId(agents: AgentConfig[]): string {
  const used = new Set(agents.map((a) => a.id))
  for (const c of 'ABCDEFGHIJKLMNOPQRSTUVWXYZ') if (!used.has(c)) return c
  return `S${agents.length}`
}

export function newAgent(role: string, agents: AgentConfig[], r: Registries, research = false): AgentConfig {
  const policy = policiesForRole(role, research, r)[0]
  const tools = toolsForRole(role, r)
  const id = role === 'seller' ? nextSellerId(agents) : agents.some((a) => a.id === role) ? `${role}${agents.length}` : role
  return {
    id,
    role,
    policy: policy?.kind === 'llm' ? { kind: 'llm', llm: defaultLlm(r, research ? undefined : id) as never } : { kind: policy?.kind ?? 'constant', params: policyParams(policy) },
    capability: research || tools.length === 0 ? null : { grants: tools.filter((t) => !OPTIONAL_TOOLS.has(t.id)).map((t) => defaultGrant(t, r.root)), max_queries_per_round: 3 },
  }
}

export function starterDraft(kind: RunConfig['kind'], r: Registries): RunConfig {
  const base = defaultFor(r.root, r.root) as Partial<RunConfig>
  const research = kind === 'research'
  const scenario = research ? undefined : r.scenarios.find((s) => s.status === 'frozen')
  let agents: AgentConfig[] = []
  agents = addAgent('seller', agents, r, research)
  agents = addAgent('seller', agents, r, research)
  return {
    ...base,
    kind,
    scenario_id: scenario?.id ?? null,
    scenario_hash: scenario?.hash ?? null,
    condition_id: null,
    prompt_variant_id: null,
    split: research ? 'pilot' : 'exploratory',
    seeds: research ? [] : [7],
    agents,
    channels: research ? null : { public: true, private_pairs: true, news: true },
    transparency: research ? null : 'rival_last_round',
    call_mode: research ? null : (base.call_mode ?? 'talk_and_act'),
    clock: base.clock ?? { mode: 'sync' },
    budget: base.budget ?? { max_usd: 0.5 },
  }
}

type Locked = { path: Path; nullable: boolean }

/** Every path that the schema marks x-research-locked. '*' stands for any array item. */
export function lockedPaths(root: JSONSchema, s: JSONSchema = root, path: Path = [], depth = 0): Locked[] {
  if (depth > 12) return []
  const { schema, nullable } = unwrapNull(root, s)
  if (schema['x-research-locked']) return [{ path, nullable }]
  const props = Object.entries(schema.properties ?? {}).flatMap(([k, p]) => lockedPaths(root, p, [...path, k], depth + 1))
  const items = schema.items ? lockedPaths(root, resolve(root, schema.items), [...path, '*'], depth + 1) : []
  return [...props, ...items]
}

function expand(d: unknown, path: Path): Path[] {
  const i = path.indexOf('*')
  if (i === -1) return [path]
  const list = getAt(d, path.slice(0, i))
  return Array.isArray(list) ? list.flatMap((_, j) => expand(d, [...path.slice(0, i), j, ...path.slice(i + 1)])) : []
}

/**
 * A research config as the backend takes it. Every locked field that may be null is null, because the condition and
 * the seed map set it (memory.summary too, since 0.31.0). A locked field that may not be null gets the condition's value.
 */
export function clearLocked(d: RunConfig, c: Condition | undefined, root: JSONSchema): RunConfig {
  let out = d
  for (const { path, nullable } of lockedPaths(root)) {
    for (const p of expand(out, path)) {
      if (nullable) {
        if (getAt(out, p.slice(0, -1)) != null) out = setAt(out, p, null)
      } else {
        const v = conditionValue(out, c, p)
        if (v !== undefined) out = setAt(out, p, v)
      }
    }
  }
  return out
}

/** Research agents for these seller ids: each keeps its policy from `prev`, a new one starts as an LLM; a host stays scripted. */
export function conditionSellers(ids: string[], prev: AgentConfig[], r: Registries): AgentConfig[] {
  const byId = new Map(prev.map((a) => [a.id, a]))
  const sellers = ids.map((id): AgentConfig => {
    const p = byId.get(id)
    return { id, role: 'seller', policy: p?.role === 'seller' ? p.policy : { kind: 'llm', llm: defaultLlm(r) as never }, capability: null }
  })
  const host = prev.find((a) => a.role === 'host')
  return [...sellers, ...(host ? [{ ...host, policy: { kind: 'scripted_host' as const }, capability: null }] : [])]
}

/** A research draft for a condition: its sellers, the scripted host, and every locked field cleared. */
export function applyCondition(d: RunConfig, c: Condition, r: Registries): RunConfig {
  const agents = conditionSellers(Object.keys(c.capabilities), d.agents, r)
  const split = d.split === 'dev' && devRefusal(c) ? { split: 'pilot' as const, seeds: [] } : {}
  return clearLocked({ ...d, ...split, condition_id: c.id, agents }, c, r.root)
}

/** Seeds by prompt variant id, or the rule when no seed is chosen yet. */
function variantsBySeed(d: RunConfig, c: Condition): Record<string, string> {
  return Object.fromEntries(
    c.prompt_variants.map((v, i) => {
      const seeds = d.seeds.filter((s) => variantFor(c, s).id === v.id)
      return [v.id, d.seeds.length ? (seeds.length ? `for ${seeds.length === 1 ? 'seed' : 'seeds'} ${seeds.join(', ')}` : 'no chosen seed') : variantSeedRule(c, i)]
    }),
  )
}

/** The value that the condition or the seed map sets for a locked path, or undefined. */
export function conditionValue(d: RunConfig, c: Condition | undefined, path: Path): unknown {
  if (!c) return undefined
  const [head, i, ...rest] = path
  if (head === 'scenario_id') return 'One scenario per seed, from the seed map'
  if (head === 'prompt_variant_id') return variantsBySeed(d, c)
  if (head === 'channels') return c.channels
  if (head === 'transparency') return c.transparency
  if (head === 'call_mode') return c.call_mode
  if (head === 'observation_mode') return c.observation_mode
  if (head === 'memory' && i === 'summary') return c.memory_summary
  if (head === 'limits') return c.limits
  if (head === 'agents' && typeof i === 'number') {
    const id = d.agents[i]?.id ?? ''
    if (rest[0] === 'capability') return c.capabilities[id] ?? null
    const prompt = rest.join('.').match(/^policy\.llm\.(system_prompt|talk_prompt)$/)?.[1] as 'system_prompt' | 'talk_prompt' | undefined
    if (prompt) return Object.fromEntries(c.prompt_variants.map((v) => [v.id, v.sellers[id]?.[prompt] ?? (prompt === 'talk_prompt' ? 'none: the talk call uses the system prompt' : 'none')]))
  }
  return undefined
}

/** C2a needs one model for every seller, C2b a different model family for each. Returns a note when the draft breaks that. */
export function sameModelNote(d: Pick<RunConfig, 'agents'>, c: Condition | undefined): string | null {
  if (!c || c.same_model_for_all_sellers == null) return null
  const sellers = d.agents.filter((a) => a.role === 'seller')
  const key = (a: AgentConfig, k: 'model' | 'model_family') => (a.policy.kind === 'llm' ? (a.policy.llm?.[k] ?? null) : `scripted:${a.policy.kind}`)
  if (c.same_model_for_all_sellers) return new Set(sellers.map((a) => key(a, 'model'))).size > 1 ? `${c.id} needs the same model for every seller.` : null
  const families = sellers.map((a) => key(a, 'model_family'))
  return families.includes(null) || new Set(families).size < families.length ? `${c.id} needs a different model family for each seller. Set "Model family" under More LLM settings.` : null
}
