import { useQuery } from '@tanstack/react-query'
import { api, unwrap } from './client'
import type { components } from './schema'
import type { JSONSchema } from '@/lib/jsonSchema'

type S = components['schemas']
export type RunConfig = S['RunConfig']
export type AgentConfig = S['AgentConfig']
export type Capability = S['Capability']
export type ToolGrant = S['ToolGrant']
export type RoleSpec = S['RoleSpec']
export type ToolSpec = S['ToolSpec']
export type ToolDefinition = S['ToolDefinition']
export type PolicyType = S['PolicyType']
export type Preset = S['Preset']
export type CostEstimate = S['CostEstimate']
export type PromptPreview = S['PromptPreview']
export type ScenarioSummary = S['ScenarioSummary']
export type Condition = S['Condition']
export type SeedSplits = S['SeedSplits']
export type FreezeStatus = S['FreezeStatus']

/** The prompt variant of a seed, as the backend picks it (research.py): prompt_variants[seed % n]. */
export const variantFor = (c: Condition, seed: number) => c.prompt_variants[seed % c.prompt_variants.length]!
/** A frozen condition whose file changed after the freeze. The server refuses every research run on it (409 hash_mismatch). */
export const changedSinceFreeze = (c: Pick<Condition, 'frozen_hash' | 'hash'>) => c.frozen_hash != null && c.frozen_hash !== c.hash
/** Why the server refuses dev seeds for this condition (409 condition_not_frozen), or null. */
export const devRefusal = (c: Pick<Condition, 'id' | 'status'>) => (c.status === 'frozen' ? null : `${c.id} is a draft; dev needs a frozen condition`)

/** Which seeds get variant i, in words. */
export const variantSeedRule = (c: Condition, i: number) => {
  const n = c.prompt_variants.length
  return n === 2 ? (i === 0 ? 'even seeds' : 'odd seeds') : `seeds with seed % ${n} = ${i}`
}

// Registries change only when the backend restarts with new plug-ins.
const REGISTRY = { staleTime: 5 * 60_000 }

export const useRunConfigSchema = () =>
  useQuery({ queryKey: ['schema', 'run-config'], queryFn: async () => (await unwrap(api.GET('/schema/run-config'))) as JSONSchema, ...REGISTRY })
export const useRoles = () => useQuery({ queryKey: ['roles'], queryFn: () => unwrap(api.GET('/roles')), ...REGISTRY })
export const useTools = () => useQuery({ queryKey: ['tools'], queryFn: () => unwrap(api.GET('/tools')), ...REGISTRY })
export const usePolicies = () => useQuery({ queryKey: ['policies'], queryFn: () => unwrap(api.GET('/policies')), ...REGISTRY })
export const useScenarios = () => useQuery({ queryKey: ['scenarios'], queryFn: () => unwrap(api.GET('/scenarios')) })
export const useConditions = () => useQuery({ queryKey: ['conditions'], queryFn: () => unwrap(api.GET('/conditions')), ...REGISTRY })
export const useCondition = (id: string) => useQuery({ queryKey: ['condition', id], queryFn: () => unwrap(api.GET('/conditions/{condition_id}', { params: { path: { condition_id: id } } })) })
export const useFreeze = () => useQuery({ queryKey: ['freeze'], queryFn: () => unwrap(api.GET('/freeze')) })
export const useSeeds = () => useQuery({ queryKey: ['seeds'], queryFn: () => unwrap(api.GET('/seeds')) })
/** SIM_DEFAULT_* from the server: they only pre-fill exploratory agents (DECISIONS-9 §16). */
export const useDefaults = () => useQuery({ queryKey: ['defaults'], queryFn: () => unwrap(api.GET('/defaults')), ...REGISTRY })
export type Defaults = S['Defaults']
// retryOnMount false: a 501 not_built (presets are not built in v1) is not asked again on each New run visit.
export const usePresets = () => useQuery({ queryKey: ['presets'], queryFn: () => unwrap(api.GET('/presets')), retryOnMount: false })

export const estimateRun = (body: RunConfig) => unwrap(api.POST('/runs/estimate', { body }))
export const startRuns = (body: RunConfig) => unwrap(api.POST('/runs', { body }))
export const savePreset = (name: string, config: RunConfig, notes?: string) => unwrap(api.POST('/presets', { body: { name, config, notes } }))
export const previewPrompt = (run_config: RunConfig, seller_id: string) =>
  unwrap(api.POST('/prompts/preview', { body: { run_config, seller_id, t: 1, purpose: 'act' } }))
