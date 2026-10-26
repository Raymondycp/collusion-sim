import { flatten } from '@/lib/jsonSchema'

/** One requested change in a clone: a config field (dot path, as flatten() gives it) and the new value as typed. */
export type OverrideRow = { path: string; text: string }

/** "agents.0.policy.llm.temperature" -> "/agents/0/policy/llm/temperature" */
export const toPointer = (dot: string) => `/${dot.split('.').map((k) => k.replace(/~/g, '~0').replace(/\//g, '~1')).join('/')}`

/** Leaf fields of a config that a clone may change, with their current values. */
export function overridable(config: unknown): [string, unknown][] {
  // Ids and links describe where the run came from; they are not settings.
  const skip = /^(preset_id|scenario_hash|name)$|\.id$/
  return Object.entries(flatten(config))
    .filter(([k]) => !skip.test(k))
    .sort(([a], [b]) => a.localeCompare(b))
}

/** Reads the typed text with the type of the current value. Returns the value, or the problem with the text. */
export function parseValue(text: string, current: unknown): { value: unknown } | { error: string } {
  const t = text.trim()
  if (typeof current === 'number') return t !== '' && Number.isFinite(Number(t)) ? { value: Number(t) } : { error: 'Enter a number.' }
  if (typeof current === 'boolean') return t === 'true' || t === 'false' ? { value: t === 'true' } : { error: 'Enter true or false.' }
  if (typeof current === 'string') return { value: text }
  try {
    return { value: JSON.parse(t) }
  } catch {
    return { error: 'Enter JSON, for example null, 3 or "text".' }
  }
}

export function toOverrides(rows: OverrideRow[], fields: Map<string, unknown>): { overrides: Record<string, unknown> } | { errors: Record<number, string> } {
  const errors: Record<number, string> = {}
  const overrides: Record<string, unknown> = {}
  rows.forEach((r, i) => {
    if (!fields.has(r.path)) return void (errors[i] = 'Select a field.')
    if (toPointer(r.path) in overrides) return void (errors[i] = 'This field is changed twice.')
    const v = parseValue(r.text, fields.get(r.path))
    if ('error' in v) errors[i] = v.error
    else overrides[toPointer(r.path)] = v.value
  })
  return Object.keys(errors).length ? { errors } : { overrides }
}
