import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, unwrap } from './client'
import type { components } from './schema'

export type ArchiveKind = 'runs' | 'experiments'
export type ArchiveResult = components['schemas']['ArchiveResult']
export type ArchiveAction = 'archive' | 'unarchive'
/** Ids, or every record the server lists now (DECISIONS-29). `all` skips runs that have not ended and names them in `skipped`. */
export type ArchiveTarget = { ids: string[] } | { all: true }

const post = (kind: ArchiveKind, action: ArchiveAction, body: ArchiveTarget): Promise<ArchiveResult> => {
  if (kind === 'runs') return unwrap(action === 'archive' ? api.POST('/runs/archive', { body }) : api.POST('/runs/unarchive', { body }))
  return unwrap(action === 'archive' ? api.POST('/experiments/archive', { body }) : api.POST('/experiments/unarchive', { body }))
}

/** One mutation for archive and restore of a list. Both lists (with and without archived records) refresh afterwards. */
export function useArchive(kind: ArchiveKind) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ action, target }: { action: ArchiveAction; target: ArchiveTarget }) => post(kind, action, target),
    onSettled: () => qc.invalidateQueries({ queryKey: [kind === 'runs' ? 'runs' : 'experiments'] }),
  })
}
