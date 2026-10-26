import { useQuery } from '@tanstack/react-query'
import { api, unwrap } from './client'
import type { components } from './schema'

export type TrailLine = components['schemas']['TrailLine']

/** One part of a message, as the panel shows it. A part kind the panel does not know keeps its raw JSON. */
export type TrailPart =
  | { kind: 'system' | 'user' | 'text' | 'thinking'; text: string }
  | { kind: 'tool-return' | 'tool-call'; tool: string; text: string }
  | { kind: 'unknown'; partKind: string; text: string }

export type TrailMessage = { role: 'request' | 'answer' | 'unknown'; parts: TrailPart[] }

export type TrailTool = { name: string; description: string; schema: string }

/** The line, with `tools` and `messages` (Pydantic AI JSON) parsed for the panel. Null where the line has none. */
export type TrailView = { line: TrailLine; tools: TrailTool[] | null; messages: TrailMessage[] | null }

type Raw = Record<string, unknown>

/** Pydantic AI keeps tool-call args as a JSON string or an object: a string shows as it is. */
const show = (x: unknown) => (typeof x === 'string' ? x : JSON.stringify(x ?? null, null, 2))

function part(p: Raw): TrailPart {
  switch (p.part_kind) {
    case 'system-prompt':
      return { kind: 'system', text: show(p.content) }
    case 'user-prompt':
      return { kind: 'user', text: show(p.content) }
    case 'text':
      return { kind: 'text', text: show(p.content) }
    case 'thinking':
      return { kind: 'thinking', text: show(p.content) }
    case 'tool-return':
      return { kind: 'tool-return', tool: String(p.tool_name), text: show(p.content) }
    case 'tool-call':
      return { kind: 'tool-call', tool: String(p.tool_name), text: show(p.args ?? {}) }
    default:
      return { kind: 'unknown', partKind: String(p.part_kind ?? 'no part_kind'), text: JSON.stringify(p, null, 2) }
  }
}

function message(m: Raw): TrailMessage {
  if (m.kind === 'request' || m.kind === 'response') return { role: m.kind === 'request' ? 'request' : 'answer', parts: ((m.parts as Raw[] | undefined) ?? []).map(part) }
  return { role: 'unknown', parts: [{ kind: 'unknown', partKind: `message ${String(m.kind ?? '')}`.trim(), text: JSON.stringify(m, null, 2) }] }
}

export function trailView(line: TrailLine): TrailView {
  return {
    line,
    tools: line.tools ? (line.tools as Raw[]).map((t) => ({ name: String(t.name), description: String(t.description ?? ''), schema: JSON.stringify(t.parameters_json_schema ?? {}, null, 2) })) : null,
    messages: line.messages ? (line.messages as Raw[]).map(message) : null,
  }
}

/** A stored call does not change, also in a live run: the server writes the llm_call event after the reply. */
export const useCallTrail = (runId: string, seq: number | null) =>
  useQuery({
    queryKey: ['trail', runId, seq],
    queryFn: async () => trailView(await unwrap(api.GET('/runs/{run_id}/calls/{seq}/trail', { params: { path: { run_id: runId, seq: seq! } } }))),
    enabled: seq !== null,
    staleTime: Infinity,
  })
