import { useQuery } from '@tanstack/react-query'
import { api, unwrap } from './client'
import type { components } from './schema'
import type { JSONSchema } from '@/lib/jsonSchema'

type S = components['schemas']
export type Scenario = S['Scenario']
export type ScenarioDraft = S['ScenarioDraft']
export type ScenarioEvent = S['ScenarioEvent']
export type ScenarioSummary = S['ScenarioSummary']
export type ScenarioStatus = S['ScenarioStatus']
export type Shock = S['Shock']
export type CheckReport = S['CheckReport']
export type ReferencePoint = S['ReferencePoint']
export type ScenarioMarker = S['ScenarioReference']['markers'][number]
export type DemandModel = Omit<S['DemandModel'], 'params_schema' | 'solver_schema'> & { params_schema: JSONSchema; solver_schema?: JSONSchema | null }
export type ScenarioGenerator = Omit<S['ScenarioGenerator'], 'input_schema'> & { input_schema: JSONSchema }
export type GenerateRequest = S['GenerateRequest']
export type GenerateResult = S['GenerateResult']
export type MarketPreview = S['MarketPreview']

const REGISTRY = { staleTime: 5 * 60_000 }
const path = (id: string) => ({ params: { path: { scenario_id: id } } })

export const useScenarioList = (status?: ScenarioStatus) =>
  useQuery({ queryKey: ['scenarios', status ?? 'all'], queryFn: () => unwrap(api.GET('/scenarios', { params: { query: { status } } })) })
export const useScenario = (id: string | undefined) =>
  useQuery({ queryKey: ['scenario', id], enabled: id !== undefined, queryFn: () => unwrap(api.GET('/scenarios/{scenario_id}', path(id!))) })
export const useScenarioReference = (id: string | undefined) =>
  useQuery({ queryKey: ['scenario', id, 'reference'], enabled: id !== undefined, queryFn: () => unwrap(api.GET('/scenarios/{scenario_id}/reference', path(id!))) })
export const useDemandModels = () =>
  useQuery({ queryKey: ['demand-models'], queryFn: async () => (await unwrap(api.GET('/demand-models'))) as DemandModel[], ...REGISTRY })
export const useGenerators = () =>
  useQuery({ queryKey: ['scenario-generators'], queryFn: async () => (await unwrap(api.GET('/scenario-generators'))) as ScenarioGenerator[], ...REGISTRY })

export const createScenario = (body: ScenarioDraft) => unwrap(api.POST('/scenarios', { body }))
export const replaceScenario = (id: string, body: ScenarioDraft) => unwrap(api.PUT('/scenarios/{scenario_id}', { ...path(id), body }))
export const deleteScenario = (id: string) => unwrap(api.DELETE('/scenarios/{scenario_id}', path(id)))
export const checkScenario = (id: string) => unwrap(api.POST('/scenarios/{scenario_id}/check', path(id)))
export const freezeScenario = (id: string, approved_by: string) => unwrap(api.POST('/scenarios/{scenario_id}/freeze', { ...path(id), body: { approved_by } }))
export const solveDemand = (modelId: string, targets: Record<string, unknown>) =>
  unwrap(api.POST('/demand-models/{model_id}/solve', { params: { path: { model_id: modelId } }, body: targets }))
export const generateScenario = (body: GenerateRequest) => unwrap(api.POST('/scenarios/generate', { body }))
export const previewMarket = (d: ScenarioDraft) =>
  unwrap(api.POST('/market/preview', { body: { market: d.market, n_sellers: d.n_sellers, rounds: d.horizon.max_rounds, events: d.events } }))
