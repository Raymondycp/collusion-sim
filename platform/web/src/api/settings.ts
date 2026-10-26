import { useQuery } from '@tanstack/react-query'
import { api, unwrap } from './client'
import type { Defaults } from './runConfig'
import type { components } from './schema'

type S = components['schemas']

/** UI values override the .env values; .env only gives the first values (DECISIONS-11 §1). */
export const saveDefaults = (d: Defaults) => unwrap(api.PUT('/defaults', { body: d }))

/** A text file that the server stores, checks and hashes: a condition file or a research file. */
export type StoredFile = S['StoredFile']

export type ResearchFile = 'decision-rule' | 'judge-rubric' | 'models'
/** `key` is the freeze input name; `stage` is the freeze stage that holds the file (DECISIONS-11 §2.5: the rubric fixes the judge items before dev judging). */
export const RESEARCH_FILES: { id: ResearchFile; key: string; stage: StageId; title: string; file: string; about: string }[] = [
  { id: 'decision-rule', key: 'decision_rule', stage: 'rule', title: 'Decision rule', file: 'conditions/decision-rule.yaml', about: 'The pre-registered rule structure and the selection criterion (P2). Freeze stage 2 needs it. It can change until a test cell is used; a change after stage 2 locks the test seeds until stage 2 is frozen again.' },
  { id: 'judge-rubric', key: 'judge_rubric', stage: 'conditions', title: 'Judge rubric', file: 'conditions/judge-rubric.yaml', about: 'The rubric that both judge backends use. Freeze stage 1 needs it; wording work runs on pilot seeds before.' },
  // Added after the real-backend flow test (FLOWS-web D2): the freeze page said "Add them first (Research files)" for this file, and the page had no editor for it.
  { id: 'models', key: 'models', stage: 'conditions', title: 'Seller models', file: 'conditions/models.yaml', about: 'The model and the sampling settings of each seller for each condition, and the judge model. Freeze stage 1 needs it. Dev and test runs must use these models. The server fills base_url and protocol from the provider profile when you save. The template is docs/team/templates/models.template.yaml.' },
]
export const researchFileKey = (id: ResearchFile) => ['research-file', id]
export const useResearchFile = (id: ResearchFile) =>
  useQuery({ queryKey: researchFileKey(id), queryFn: () => unwrap(api.GET('/research/{name}', { params: { path: { name: id } } })) })
/** `invalidated_stage` is 'rule' when this save made a valid rule stage not valid (the decision rule may change until a test cell is used). */
export const saveResearchFile = (id: ResearchFile, text: string) => unwrap(api.PUT('/research/{name}', { params: { path: { name: id } }, body: { text } }))

export const conditionFileKey = (id: string) => ['condition-file', id]
export const useConditionFile = (id: string) =>
  useQuery({ queryKey: conditionFileKey(id), queryFn: () => unwrap(api.GET('/conditions/{condition_id}/file', { params: { path: { condition_id: id } } })) })
/** 409 `condition_frozen` once the condition is frozen. Every save writes the YAML file, so git shows the diff. */
export const saveConditionFile = (id: string, text: string) =>
  unwrap(api.PUT('/conditions/{condition_id}/file', { params: { path: { condition_id: id } }, body: { text } }))

/** Two freeze stages (DECISIONS-11 §2.5): `conditions` (with the judge rubric) opens dev, `rule` (the decision rule) opens test. */
export type StageId = keyof S['FreezeStatus']['stages']
export type StageStatus = S['FreezeStage']
/** Input keys as `GET /freeze` `inputs` names them: the decision rule belongs to stage `rule`, every other input to stage `conditions`. */
export const stageOf = (input: string): StageId => (input === 'decision_rule' ? 'rule' : 'conditions')

/** The top level is stage `rule`, the test gate; `inputs` holds every input with its hash now (null: a research file is missing). */
export type FreezePreview = S['FreezeStatus']
export const FREEZE_KEY = ['freeze']
export const useFreezePreview = () => useQuery({ queryKey: FREEZE_KEY, queryFn: () => unwrap(api.GET('/freeze')) })
export const postFreeze = (stage: StageId, approved_by: string) => unwrap(api.POST('/freeze', { body: { stage, approved_by } }))
