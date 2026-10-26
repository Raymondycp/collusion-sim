// The subset of JSON Schema 2020-12 that the run-config form reads. Anything else falls back to a JSON editor.
export type JSONSchema = {
  $ref?: string
  $defs?: Record<string, JSONSchema>
  type?: string | string[]
  title?: string
  description?: string
  properties?: Record<string, JSONSchema>
  required?: string[]
  items?: JSONSchema
  enum?: unknown[]
  const?: unknown
  oneOf?: JSONSchema[]
  anyOf?: JSONSchema[]
  default?: unknown
  minimum?: number
  maximum?: number
  exclusiveMinimum?: number
  minLength?: number
  maxLength?: number
  minItems?: number
  maxItems?: number
  prefixItems?: JSONSchema[]
  pattern?: string
  format?: string
  additionalProperties?: boolean | JSONSchema
  'x-research-locked'?: boolean
}

export type FieldKind = 'object' | 'array' | 'enum' | 'string' | 'number' | 'integer' | 'boolean' | 'json'

/** Follows `#/$defs/...` refs. Keywords next to a $ref (title, description, x-research-locked) win over the target. */
export function resolve(root: JSONSchema, s: JSONSchema): JSONSchema {
  let cur = s
  for (let i = 0; cur.$ref && i < 20; i++) {
    const name = cur.$ref.replace(/^#\/(\$defs|definitions)\//, '')
    const target = root.$defs?.[name]
    if (!target) break
    const { $ref: _ref, ...rest } = cur
    cur = { ...target, ...rest }
  }
  return cur
}

/**
 * Splits a nullable schema (`oneOf: [X, null]` or `type: [x, "null"]`) into X and a flag. A union of a boolean and an enum (the
 * `thinking` setting of 0.32.0: true, false, minimal ... xhigh) becomes one enum of its values, so a select shows it, not raw JSON.
 */
export function unwrapNull(root: JSONSchema, s: JSONSchema): { schema: JSONSchema; nullable: boolean } {
  const r = resolve(root, s)
  const branches = r.oneOf ?? r.anyOf
  if (branches) {
    const rest = branches.filter((b) => !(b.type === 'null'))
    const [a, b] = rest.map((x) => resolve(root, x))
    if (rest.length === 2 && a && b) {
      const [bool, en] = a.type === 'boolean' && !a.enum ? [a, b] : b.type === 'boolean' && !b.enum ? [b, a] : [null, null]
      if (bool && en?.enum?.length) {
        const { oneOf: _o, anyOf: _a, ...outer } = r
        return { schema: { ...outer, enum: [true, false, ...en.enum] }, nullable: rest.length < branches.length }
      }
    }
    if (rest.length === 1 && rest.length < branches.length) {
      const { oneOf: _o, anyOf: _a, ...outer } = r
      return { schema: { ...resolve(root, rest[0]!), ...outer }, nullable: true }
    }
  }
  if (Array.isArray(r.type) && r.type.includes('null')) {
    const types = r.type.filter((t) => t !== 'null')
    return { schema: { ...r, type: types.length === 1 ? types[0] : types, enum: r.enum?.filter((e) => e !== null) }, nullable: true }
  }
  return { schema: r, nullable: false }
}

export function kindOf(s: JSONSchema): FieldKind {
  if (s.enum && s.enum.length > 0) return 'enum'
  const t = Array.isArray(s.type) ? undefined : s.type
  if (t === 'object' || (!t && s.properties)) return s.properties ? 'object' : 'json'
  if (t === 'array') return 'array'
  if (t === 'string' || t === 'number' || t === 'integer' || t === 'boolean') return t
  return 'json'
}

/** A value for a new field: the schema default, else an object built from its required properties. */
export function defaultFor(root: JSONSchema, s: JSONSchema): unknown {
  const { schema, nullable } = unwrapNull(root, s)
  if (schema.default !== undefined) return structuredClone(schema.default)
  if (nullable) return null
  const k = kindOf(schema)
  if (k === 'object') {
    const out: Record<string, unknown> = {}
    for (const [name, p] of Object.entries(schema.properties ?? {})) {
      const v = (schema.required ?? []).includes(name) ? defaultFor(root, p) : resolve(root, p).default
      if (v !== undefined) out[name] = structuredClone(v)
    }
    return out
  }
  if (k === 'array') return []
  if (k === 'enum') return schema.enum![0]
  if (k === 'boolean') return false
  return undefined
}

export type Path = (string | number)[]
export const pathKey = (p: Path) => p.join('.')

export function getAt(obj: unknown, path: Path): unknown {
  let cur = obj
  for (const k of path) {
    if (cur === null || typeof cur !== 'object') return undefined
    cur = (cur as Record<string | number, unknown>)[k]
  }
  return cur
}

/** Returns a copy with `value` at `path`. `undefined` removes the key. */
export function setAt<T>(obj: T, path: Path, value: unknown): T {
  if (path.length === 0) return value as T
  const [head, ...rest] = path
  const base = (Array.isArray(obj) ? [...(obj as unknown[])] : { ...(obj as object) }) as Record<string | number, unknown>
  const next = setAt(base[head!], rest, value)
  if (next === undefined && !Array.isArray(base)) delete base[head!]
  else base[head!] = next
  return base as T
}

/** Leaf values by dot path. Empty objects and arrays count as leaves. */
export function flatten(v: unknown, prefix = '', out: Record<string, unknown> = {}): Record<string, unknown> {
  if (v !== null && typeof v === 'object' && Object.keys(v).length > 0) {
    for (const [k, x] of Object.entries(v)) flatten(x, prefix ? `${prefix}.${k}` : k, out)
  } else if (prefix) {
    out[prefix] = v
  }
  return out
}

export type DiffRow = { path: string; before: unknown; after: unknown }

/** Field-by-field difference, sorted by path. A missing field reads as undefined. */
export function diffConfigs(before: unknown, after: unknown): DiffRow[] {
  const a = flatten(before)
  const b = flatten(after)
  const keys = [...new Set([...Object.keys(a), ...Object.keys(b)])].sort()
  return keys.filter((k) => JSON.stringify(a[k]) !== JSON.stringify(b[k])).map((k) => ({ path: k, before: a[k], after: b[k] }))
}
