import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { FilesPage } from '@/features/files/FilesPage'
import { filterTree, repoTree } from '@/features/files/tree'

// Monaco needs a browser. A textarea shows what the page passes to it.
vi.mock('@monaco-editor/react', () => ({
  default: ({ value, language }: { value: string; language: string }) => <textarea data-testid="editor" data-language={language} readOnly value={value} />,
  loader: { config: () => {} },
}))
vi.mock('@/features/files/monacoSetup', () => ({}))

const TREE = {
  roots: [
    {
      path: 'paper/drafts',
      entries: [
        { path: 'paper/drafts/paper.md', kind: 'file', size: 12 },
        { path: 'paper/drafts/notes', kind: 'dir', size: 0 },
        { path: 'paper/drafts/notes/a.py', kind: 'file', size: 3 },
      ],
    },
    { path: 'paper/data', entries: [{ path: 'paper/data/exp.zip', kind: 'file', size: 99 }] },
  ],
}

const server = setupServer(
  http.get('*/api/files/tree', () => HttpResponse.json(TREE)),
  http.get('*/api/files/content', ({ request }) => {
    const path = new URL(request.url).searchParams.get('path')
    return path === 'paper/drafts/paper.md' ? new HttpResponse('# Hello\n\nbody text', { headers: { 'content-type': 'text/markdown' } }) : HttpResponse.json({ code: 'not_found', message: 'No such file' }, { status: 404 })
  }),
  http.get('*/api/files/zip', ({ request }) => {
    const u = new URL(request.url)
    return u.searchParams.get('member') === 'runs/run_A/episode.jsonl'
      ? new HttpResponse('{"a":1}\n')
      : HttpResponse.json({ members: [{ name: 'experiment.json', size: 5 }, { name: 'runs/run_A/episode.jsonl', size: 8 }] })
  }),
  http.get('*/api/experiments', () => HttpResponse.json([])),
)
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterAll(() => server.close())
afterEach(() => {
  cleanup()
  server.resetHandlers()
})

function renderPage() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <FilesPage />
    </QueryClientProvider>,
  )
}

describe('files page', () => {
  it('shows the tree and opens a file in a tab and the editor', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'paper/drafts' }))
    fireEvent.click(await screen.findByRole('button', { name: 'paper.md' }))
    const tab = await screen.findByRole('tab', { name: 'paper.md' })
    expect(tab).toHaveAttribute('aria-selected', 'true')
    const editor = await screen.findByTestId('editor')
    expect(editor).toHaveValue('# Hello\n\nbody text')
    expect(editor).toHaveAttribute('data-language', 'markdown')
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Hello' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Close paper.md' }))
    expect(screen.queryByRole('tab', { name: 'paper.md' })).not.toBeInTheDocument()
  })

  it('opens a member of a zip, grouped under its folders', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'paper/data' }))
    fireEvent.click(await screen.findByRole('button', { name: 'exp.zip' }))
    fireEvent.click(await screen.findByRole('button', { name: 'runs' }))
    fireEvent.click(await screen.findByRole('button', { name: 'run_A' }))
    fireEvent.click(await screen.findByRole('button', { name: 'episode.jsonl' }))
    expect(await screen.findByTestId('editor')).toHaveValue('{"a":1}\n')
  })

  it('shows the server message when a file cannot be read', async () => {
    server.use(http.get('*/api/files/content', () => HttpResponse.json({ code: 'file_too_large', message: 'The file is larger than 20 MB.' }, { status: 413 })))
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'paper/drafts' }))
    fireEvent.click(await screen.findByRole('button', { name: 'paper.md' }))
    expect(await screen.findByText('The file is larger than 20 MB.')).toBeInTheDocument()
  })

  it('filters by file name', async () => {
    renderPage()
    await screen.findByRole('button', { name: 'paper/drafts' })
    fireEvent.change(screen.getByLabelText('Filter files'), { target: { value: 'a.py' } })
    const list = screen.getByRole('complementary', { name: 'Library' })
    expect(within(list).getByRole('button', { name: 'a.py' })).toBeInTheDocument()
    expect(within(list).queryByRole('button', { name: 'paper.md' })).not.toBeInTheDocument()
  })
})

describe('tree builder', () => {
  it('puts folders first, marks zips and filters', () => {
    const nodes = repoTree(TREE.roots as never)
    expect(nodes[0]!.children.map((n) => n.name)).toEqual(['notes', 'paper.md'])
    expect(nodes[1]!.children[0]!.zip).toBe('paper/data/exp.zip')
    expect(filterTree(nodes, 'nothing')).toEqual([])
  })
})
