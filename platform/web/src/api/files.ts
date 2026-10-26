import { useQuery } from '@tanstack/react-query'
import { API_BASE, ApiError, api, unwrap } from './client'
import type { components } from './schema'

type S = components['schemas']
export type FilesTree = S['FilesTree']
export type FileEntry = FilesTree['roots'][number]['entries'][number]
export type ZipMember = S['FilesZip']['members'][number]

/** Larger text is cut here, so the editor stays usable. The server refuses a file over 20 MB (413). */
export const MAX_SHOWN_CHARS = 5_000_000

export const IMAGE_EXT = ['png', 'jpg', 'jpeg', 'svg']
export const extOf = (name: string) => (name.includes('.') ? name.slice(name.lastIndexOf('.') + 1).toLowerCase() : '')

export const fileUrl = (path: string) => `${API_BASE}/files/content?path=${encodeURIComponent(path)}`
const zipUrl = (path: string, member?: string) =>
  `${API_BASE}/files/zip?path=${encodeURIComponent(path)}${member === undefined ? '' : `&member=${encodeURIComponent(member)}`}`

/** The text of a response, or an ApiError with the server's short message (Problem JSON). */
async function textOf(url: string): Promise<string> {
  let r: Response
  try {
    r = await globalThis.fetch(new URL(url, location.origin)) // Node tests need an absolute URL
  } catch {
    throw new ApiError(0, 'network', 'The backend did not answer. Is it running on 127.0.0.1:8000?')
  }
  if (!r.ok) {
    const body = await r.json().catch(() => null)
    const message = body && typeof body === 'object' && typeof body.message === 'string' ? body.message : `Request failed (HTTP ${r.status}).`
    throw new ApiError(r.status, body?.code ?? 'http', message)
  }
  return r.text()
}

export type Shown = { text: string; truncated: boolean }
const cut = (text: string): Shown => (text.length > MAX_SHOWN_CHARS ? { text: text.slice(0, MAX_SHOWN_CHARS), truncated: true } : { text, truncated: false })

/** Where an open tab reads from. Repo files and zip members come from the file routes, runs from the run routes. */
export type FileSource =
  | { kind: 'repo'; path: string }
  | { kind: 'zip'; zip: string; member: string }
  | { kind: 'run-meta'; runId: string }
  | { kind: 'run-episode'; runId: string }

export const sourceKey = (s: FileSource) =>
  s.kind === 'repo' ? `repo:${s.path}` : s.kind === 'zip' ? `zip:${s.zip}!${s.member}` : s.kind === 'run-meta' ? `meta:${s.runId}` : `episode:${s.runId}`

async function load(s: FileSource): Promise<Shown> {
  if (s.kind === 'repo') return cut(await textOf(fileUrl(s.path)))
  if (s.kind === 'zip') return cut(await textOf(zipUrl(s.zip, s.member)))
  if (s.kind === 'run-meta') return cut(JSON.stringify(await unwrap(api.GET('/runs/{run_id}', { params: { path: { run_id: s.runId } } })), null, 2))
  return cut(await textOf(`${API_BASE}/runs/${encodeURIComponent(s.runId)}/episode.jsonl`))
}

export const useFileText = (s: FileSource, enabled = true) =>
  useQuery({ queryKey: ['file', sourceKey(s)], enabled, queryFn: () => load(s), staleTime: 30_000 })

export const useFilesTree = () => useQuery({ queryKey: ['files-tree'], queryFn: () => unwrap(api.GET('/files/tree')), staleTime: 30_000 })

export const useZipMembers = (path: string, enabled: boolean) =>
  useQuery({
    queryKey: ['files-zip', path],
    enabled,
    queryFn: () => unwrap(api.GET('/files/zip', { params: { query: { path } } })).then((r) => r as S['FilesZip']),
    staleTime: 30_000,
  })
