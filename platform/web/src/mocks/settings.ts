// Mock of the DECISIONS-11 settings routes (fe's proposed shapes, src/api/settings.ts). Freeze, research files and condition files share state here.
import { http, HttpResponse } from 'msw/http'
import type { Defaults } from '@/api/runConfig'
import { stageOf, type FreezePreview, type ResearchFile, type StageId, type StageStatus, type StoredFile } from '@/api/settings'
import type { Problem } from '@/api/types'
import { mockResearchConditions, mockSeeds, setMockDefaults, setMockResearchState } from './registry'
import { realFreeze } from './researchFixtures'

const problem = (status: number, code: string, message: string, errors: Problem['errors'] = []) =>
  HttpResponse.json({ title: message, status, code, message, errors }, { status, headers: { 'Content-Type': 'application/problem+json' } })

/** Not a real hash: a stable fake so that a changed text gives a changed value. */
function fakeHash(text: string): string {
  let h = 0
  for (const ch of text) h = (h * 31 + ch.charCodeAt(0)) >>> 0
  return `sha256:mock${h.toString(16).padStart(8, '0')}`
}

const stored = (name: string, text: string | null): StoredFile => ({ name, text, hash: text === null ? null : fakeHash(text), updated_at: text === null ? null : '2026-10-21T12:00:00Z' })

let research: Record<ResearchFile, StoredFile>
let conditionTexts: Record<string, string>
/** What frozen.json holds per stage (0.13.0 `{stages: {conditions, rule}}`). */
type Stored = { frozen_at: string; approved_by: string; hashes: Record<string, string> }
let stages: Record<StageId, Stored | null>
export function resetMockSettings() {
  research = { 'decision-rule': stored('conditions/decision-rule.yaml', null), 'judge-rubric': stored('conditions/judge-rubric.yaml', null), models: stored('conditions/models.yaml', null) }
  conditionTexts = {}
  stages = { conditions: null, rule: null }
}
resetMockSettings()

/** A YAML-like text of a condition, enough for the editor. The real file keeps anchors and prompt paths. */
function conditionText(id: string): string | null {
  if (conditionTexts[id]) return conditionTexts[id]
  const c = mockResearchConditions().find((x) => x.id === id)
  if (!c) return null
  const { hash: _h, prompt_variants: _p, capabilities: _c, ...rest } = c
  return Object.entries(rest)
    .map(([k, v]) => `${k}: ${JSON.stringify(v)}`)
    .join('\n')
}

/** 0.13.0 input keys (`ResearchStore.current_hashes`); a missing research file is null. */
function inputs(): Record<string, string | null> {
  return {
    ...Object.fromEntries(mockResearchConditions().map((c) => [`condition:${c.id}`, c.hash])),
    seeds: mockSeeds.hash ?? null,
    features: 'sha256:mockfeatures01',
    decision_rule: research['decision-rule'].hash,
    judge_rubric: research['judge-rubric'].hash,
    models: research.models.hash,
  }
}

/** A stage is valid while every hash it stored matches the files now; stage rule also needs a valid stage conditions. */
function stageView(id: StageId): StageStatus {
  const s = stages[id]
  if (!s) return { frozen: false, frozen_at: null, approved_by: null, valid: false, problems: stages.conditions || stages.rule ? [`stage ${id} is not frozen`] : realFreeze.stages[id].problems }
  const now = inputs()
  const changed = Object.keys(s.hashes).filter((k) => now[k] !== s.hashes[k])
  const problems = [...changed.map((k) => `${k} changed after the freeze`), ...(id === 'rule' && !stageView('conditions').valid ? ['stage conditions is not valid'] : [])]
  return { frozen: true, frozen_at: s.frozen_at, approved_by: s.approved_by, valid: problems.length === 0, problems }
}

/** The top level is stage rule, the test gate. */
function freezeView(): FreezePreview {
  const conditions = stageView('conditions')
  const rule = stageView('rule')
  if (!conditions.frozen) return { ...realFreeze, inputs: inputs(), stages: { conditions, rule } }
  return { frozen: rule.frozen, frozen_at: rule.frozen_at, valid: rule.valid, problems: rule.problems, inputs: inputs(), stages: { conditions, rule } }
}

export const settingsHandlers = [
  http.put('/api/defaults', async ({ request }) => {
    const d = (await request.json()) as Defaults
    setMockDefaults(d)
    return HttpResponse.json(d)
  }),

  http.get('/api/research/:file', ({ params }) => {
    const f = research[params.file as ResearchFile]
    return f ? HttpResponse.json(f) : problem(404, 'not_found', `No research file ${params.file}.`)
  }),
  http.put('/api/research/:file', async ({ params, request }) => {
    const id = params.file as ResearchFile
    if (!research[id]) return problem(404, 'not_found', `No research file ${params.file}.`)
    if ((id === 'judge-rubric' || id === 'models') && stages.conditions) return problem(409, 'research_frozen', `${research[id].name} is frozen; it cannot change after the freeze.`)
    const { text } = (await request.json()) as { text: string }
    if (!text.trim()) return problem(422, 'validation_error', 'The file is empty.', [{ path: 'text', message: 'empty' }])
    if (/^\s*\t/m.test(text)) return problem(422, 'validation_error', 'Not valid YAML: line starts with a tab.', [{ path: 'text', message: 'tab indent' }])
    const ruleWasValid = stageView('rule').valid
    research[id] = stored(research[id].name, text)
    return HttpResponse.json({ ...research[id], invalidated_stage: ruleWasValid && !stageView('rule').valid ? 'rule' : null })
  }),

  http.get('/api/conditions/:id/file', ({ params }) => {
    const text = conditionText(String(params.id))
    return text === null ? problem(404, 'not_found', `Condition ${params.id} was not found.`) : HttpResponse.json(stored(`conditions/${params.id}.yaml`, text))
  }),
  http.put('/api/conditions/:id/file', async ({ params, request }) => {
    const id = String(params.id)
    const c = mockResearchConditions().find((x) => x.id === id)
    if (!c) return problem(404, 'not_found', `Condition ${id} was not found.`)
    if (c.status === 'frozen') return problem(409, 'condition_frozen', `Condition ${id} is frozen. It cannot change.`)
    const { text } = (await request.json()) as { text: string }
    if (!new RegExp(`^id: "?${id}"?$`, 'm').test(text)) return problem(422, 'validation_error', `The file must keep id: ${id}.`, [{ path: 'id', message: 'must not change' }])
    conditionTexts[id] = text
    const hash = fakeHash(text)
    const name = /^name: "?(.*?)"?$/m.exec(text)?.[1] ?? c.name
    setMockResearchState({ conditions: mockResearchConditions().map((x) => (x.id === id ? { ...x, hash, name } : x)) })
    return HttpResponse.json(stored(`conditions/${id}.yaml`, text))
  }),

  http.get('/api/freeze', () => HttpResponse.json(freezeView())),
  // 0.13.0 with DECISIONS-11 §2.5 as updated: stage conditions needs the judge rubric, stage rule the decision rule (409 freeze_incomplete); stage rule needs a valid stage conditions (409 conflict); a new stage conditions removes stage rule.
  http.post('/api/freeze', async ({ request }) => {
    const { stage, approved_by } = (await request.json()) as { stage: StageId; approved_by: string }
    if (stage !== 'conditions' && stage !== 'rule') return problem(422, 'validation_error', 'stage must be conditions or rule.', [{ path: 'stage', message: 'conditions or rule' }])
    if (!approved_by?.trim()) return problem(422, 'validation_error', 'Name the person who approves the freeze.', [{ path: 'approved_by', message: 'required' }])
    const now = inputs()
    if (stage === 'rule' && !stageView('conditions').valid) return problem(409, 'conflict', 'Stage conditions is not frozen and valid. Freeze it first.')
    const missing = Object.keys(now).filter((k) => stageOf(k) === stage && !now[k])
    if (missing.length) return problem(409, 'freeze_incomplete', `Missing: ${missing.join(', ')}.`)
    const hashes = Object.fromEntries(Object.entries(now).flatMap(([k, v]) => (stage === 'rule' || stageOf(k) === 'conditions' ? [[k, v ?? '']] : [])))
    const at = new Date().toISOString()
    if (stage === 'conditions') {
      setMockResearchState({ conditions: mockResearchConditions().map((c) => ({ ...c, status: 'frozen', frozen_hash: c.hash })) })
      stages = { conditions: { frozen_at: at, approved_by, hashes }, rule: null }
    } else stages = { ...stages, rule: { frozen_at: at, approved_by, hashes } }
    return HttpResponse.json({ ...freezeView(), hashes })
  }),
]
