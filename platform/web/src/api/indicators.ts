import { useQuery } from '@tanstack/react-query'
import { api, unwrap } from './client'
import type { components } from './schema'

type S = components['schemas']
export type IndicatorSpec = S['IndicatorSpec']
export type IndicatorInstance = S['IndicatorInstance']

export type ParamValue = number | boolean | string
export type IndicatorParams = Record<string, ParamValue>
/** One indicator on the chart. The UI keeps one instance per indicator id. */
export type IndicatorSelection = { id: string; params: IndicatorParams }

export type ParamField =
  | { name: string; title: string; type: 'integer' | 'number'; default?: number; minimum?: number; maximum?: number }
  | { name: string; title: string; type: 'boolean'; default?: boolean }
  | { name: string; title: string; type: 'string'; default?: string; enum?: string[] }

/** Reads the flat primitive properties of a params JSON Schema. Other shapes are skipped. */
export function paramFields(spec: IndicatorSpec): ParamField[] {
  const props = (spec.params_schema as { properties?: Record<string, Record<string, unknown>> }).properties ?? {}
  const out: ParamField[] = []
  for (const [name, p] of Object.entries(props)) {
    const title = typeof p.title === 'string' ? p.title : name
    const num = (k: string) => (typeof p[k] === 'number' ? (p[k] as number) : undefined)
    if (p.type === 'integer' || p.type === 'number') out.push({ name, title, type: p.type, default: num('default'), minimum: num('minimum'), maximum: num('maximum') })
    else if (p.type === 'boolean') out.push({ name, title, type: 'boolean', default: typeof p.default === 'boolean' ? p.default : undefined })
    else if (p.type === 'string') out.push({ name, title, type: 'string', default: typeof p.default === 'string' ? p.default : undefined, enum: Array.isArray(p.enum) ? (p.enum as string[]) : undefined })
  }
  return out
}

export function defaultParams(spec: IndicatorSpec): IndicatorParams {
  const out: IndicatorParams = {}
  for (const f of paramFields(spec)) if (f.default !== undefined) out[f.name] = f.default
  return out
}

export const useIndicatorSpecs = () => useQuery({ queryKey: ['indicators'], queryFn: () => unwrap(api.GET('/indicators')), staleTime: 60_000 })

/** What the chart needs from an indicator, whether it came from REST (finished run) or the socket (live run). */
export type IndicatorData = Pick<IndicatorInstance, 'run_id' | 'indicator_id' | 't' | 'outputs'>

/** Keeps rounds <= t. */
export function cutAt(r: IndicatorData, t: number): IndicatorData {
  const n = r.t.findIndex((x) => x > t)
  if (n === -1) return r
  const outputs: IndicatorData['outputs'] = {}
  for (const [k, v] of Object.entries(r.outputs)) outputs[k] = v.slice(0, n)
  return { ...r, t: r.t.slice(0, n), outputs }
}

/** A stable key for one instance: the same id and params give the same key. */
export function instanceKey(s: IndicatorSelection): string {
  return `${s.id}|${JSON.stringify(Object.fromEntries(Object.entries(s.params).sort(([a], [b]) => a.localeCompare(b))))}`
}

/** The spec output for a response name. Names with {seller} or {pair} come back expanded (delta_p.A). */
export function outputSpec(spec: IndicatorSpec, name: string) {
  return spec.outputs.find((o) => {
    const [head, tail] = o.name.split(/\{(?:seller|pair)\}/)
    return tail === undefined ? o.name === name : name.length > head!.length + tail.length && name.startsWith(head!) && name.endsWith(tail)
  })
}
