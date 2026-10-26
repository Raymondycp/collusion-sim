import { useMemo } from 'react'
import { setAt, type JSONSchema } from '@/lib/jsonSchema'
import { Form, SchemaField, type FormCtx } from '@/features/new-run/SchemaField'

type Props<T> = {
  /** Prefix of every field id and error key, so two forms on one page never share an id. */
  id: string
  legend: string
  schema: JSONSchema
  value: T
  onChange: (v: T) => void
  /** Server field errors, by dot path inside `value`. */
  errors?: Record<string, string>
}

/** A form built from one JSON Schema object, such as a demand model's params or a generator's inputs. */
export function SchemaForm<T>({ id, legend, schema, value, onChange, errors = {} }: Props<T>) {
  const ctx = useMemo<FormCtx>(
    () => ({
      root: schema,
      research: false,
      errors: Object.fromEntries(Object.entries(errors).map(([k, v]) => [k ? `${id}.${k}` : id, v])),
      set: (path, v) => onChange(setAt(value, path.slice(1), v)),
      lockedValue: () => undefined,
    }),
    [id, schema, errors, value, onChange],
  )
  return (
    <Form.Provider value={ctx}>
      <SchemaField path={[id]} name={id} schema={{ ...schema, title: legend }} value={value} />
    </Form.Provider>
  )
}
