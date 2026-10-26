/**
 * Plain words for the enums of the run config (UX review item 7). The raw value stays in the tooltip of the option or the chip,
 * and in the files, because the spec and the API use it. A path with no entry shows the raw value.
 */
export const ENUM_LABELS: Record<string, Record<string, string>> = {
  transparency: { rival_last_round: 'Rival prices: last round only', opaque: 'Rival prices: hidden' },
  call_mode: { one_call: 'One call: talk and price together', talk_and_act: 'Two calls: talk, then price' },
  observation_mode: { push: 'Push: the whole record in each call', pull: 'Pull: the agent reads with its tools', both: 'Both: pushed, and the read tools too' },
  'clock.mode': { sync: 'Rounds in step', event: 'Rounds by wake times' },
  'memory.summary': { code: 'Notes written by code', llm: 'Notes written by the model', none: 'No notes' },
}

/** The label of `value` for the field at `path` (the dotted path, for example "clock.mode"), else the value itself. */
export const enumLabel = (path: string, value: unknown): string => ENUM_LABELS[path]?.[String(value)] ?? String(value)

/** True when the path has a label for the value, so the raw value is worth showing beside it. */
export const hasEnumLabel = (path: string, value: unknown): boolean => ENUM_LABELS[path]?.[String(value)] !== undefined
