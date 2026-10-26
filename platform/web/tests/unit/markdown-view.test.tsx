import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MarkdownView } from '@/features/files/Markdown'

afterEach(cleanup)

describe('markdown view', () => {
  it('loads a relative image of a repo file through the file route', () => {
    render(<MarkdownView dir="paper/data/haiku-elastic" text={'![comic](conditions-comic.svg) ![up](../dev/a.png) ![web](https://example.org/x.png)'} />)
    expect(screen.getByRole('img', { name: 'comic' })).toHaveAttribute('src', '/api/files/content?path=paper%2Fdata%2Fhaiku-elastic%2Fconditions-comic.svg')
    expect(screen.getByRole('img', { name: 'up' })).toHaveAttribute('src', '/api/files/content?path=paper%2Fdata%2Fdev%2Fa.png')
    expect(screen.getByRole('img', { name: 'web' })).toHaveAttribute('src', 'https://example.org/x.png')
  })

  it('leaves images unchanged when no folder is given', () => {
    render(<MarkdownView text={'![comic](conditions-comic.svg)'} />)
    expect(screen.getByRole('img', { name: 'comic' })).toHaveAttribute('src', 'conditions-comic.svg')
  })
})
