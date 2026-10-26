import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { ArchitecturePage } from '@/features/docs/ArchitecturePage'
import { DataPage } from '@/features/docs/DataPage'

afterEach(cleanup)

function renderAt(path: string) {
  const router = createMemoryRouter(
    [
      { path: '/architecture', element: <ArchitecturePage /> },
      { path: '/data', element: <DataPage /> },
    ],
    { initialEntries: [path] },
  )
  render(<RouterProvider router={router} />)
}

describe('documentation pages', () => {
  it('architecture page shows the diagrams and the API table', () => {
    renderAt('/architecture')
    expect(screen.getByRole('heading', { level: 1, name: 'System architecture' })).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Architecture diagram' })).toHaveTextContent('Simulation core')
    expect(screen.getByRole('list', { name: 'Experiment pipeline' })).toHaveTextContent('Rule freeze')
    expect(screen.getByRole('columnheader', { name: 'Method and path' })).toBeInTheDocument()
  })

  it('data page shows the read-first note and links files to GitHub', () => {
    renderAt('/data')
    expect(screen.getByRole('heading', { level: 1, name: 'Data structure' })).toBeInTheDocument()
    expect(screen.getByTestId('read-first')).toHaveTextContent('HTTP 429')
    const link = screen.getAllByRole('link', { name: 'features.csv' })[0]
    expect(link).toHaveAttribute('href', 'https://github.com/Raymondycp/apart-hackathon/blob/sprint/paper/data/dev/features.csv')
    expect(screen.queryByTestId('haiku-read-first')).not.toBeInTheDocument()
  })

  it('data page shows the Haiku elastic study when the study switch selects it', () => {
    renderAt('/data?study=haiku-elastic')
    expect(screen.getByRole('link', { name: 'Haiku elastic study' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByTestId('haiku-read-first')).toHaveTextContent('no test split')
    expect(screen.queryByTestId('read-first')).not.toBeInTheDocument()
    const link = screen.getAllByRole('link', { name: 'features.csv' })[0]
    expect(link).toHaveAttribute('href', 'https://github.com/Raymondycp/apart-hackathon/blob/sprint/paper/data/haiku-elastic/dev/features.csv')
    expect(screen.queryByTestId('judge-progress')).not.toBeInTheDocument()
  })

  it('Haiku study shows the job progress that the server writes', async () => {
    const progress = { job: 'judge', where: 'Oracle server', state: 'running', valid: 90, target: 180, tries: 92, invalid: 2, cap: 300, cost_usd: 0.31, updated_utc: '2026-10-25 03:10:00' }
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(progress)))
    vi.stubGlobal('fetch', fetchMock)
    renderAt('/data?study=haiku-elastic')
    expect(await screen.findByTestId('judge-progress')).toHaveTextContent('running: 90 of 180 valid replies (50%). 92 tries, 2 invalid, cap 300.')
    expect(screen.getByRole('progressbar', { name: 'Valid judge replies' })).toHaveAttribute('aria-valuenow', '90')
    expect(fetchMock).toHaveBeenCalledWith('/status/judge-progress.json', { cache: 'no-store' })
  })
})
