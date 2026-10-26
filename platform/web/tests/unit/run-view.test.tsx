import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { useViewParams } from '@/features/run-view/viewParams'
import { TimelineBar } from '@/features/run-view/TimelineBar'
import { AboutPage } from '@/features/about/AboutPage'
import { LabelsText } from '@/features/runs/badges'
import { setTheme, useFollowSystemTheme } from '@/app/theme'

afterEach(() => {
  cleanup()
  localStorage.clear()
  document.documentElement.classList.remove('dark')
})

function ShowT() {
  const { t } = useViewParams()
  return <output>{t === null ? 'latest' : `t=${t}`}</output>
}

describe('round in the URL', () => {
  it.each([
    ['?t=7', 't=7'],
    ['?t=0', 't=0'],
    ['?t=abc', 'latest'],
    ['?t=-1', 'latest'],
    ['?t=3.5', 'latest'],
    ['?t=', 'latest'],
    ['', 'latest'],
  ])('%s gives %s', (search, shown) => {
    render(<RouterProvider router={createMemoryRouter([{ path: '/runs/r', element: <ShowT /> }], { initialEntries: [`/runs/r${search}`] })} />)
    expect(screen.getByRole('status')).toHaveTextContent(shown)
  })
})

describe('timeline', () => {
  const renderBar = (onChange = vi.fn()) => {
    render(<TimelineBar minT={1} maxT={10} t={4} onChange={onChange} markers={[]} following={false} />)
    return onChange
  }

  it('names the slider thumb and says the round in words', () => {
    renderBar()
    const thumb = screen.getByRole('slider', { name: 'Round' })
    expect(thumb).toHaveAttribute('aria-valuetext', 'Round 4 of 10')
    expect(thumb).toHaveAttribute('aria-valuenow', '4')
  })

  it('moves one round with the arrow keys and to the ends with Home and End', () => {
    const onChange = renderBar()
    const thumb = screen.getByRole('slider', { name: 'Round' })
    fireEvent.keyDown(thumb, { key: 'ArrowRight' })
    expect(onChange).toHaveBeenLastCalledWith(5)
    fireEvent.keyDown(thumb, { key: 'ArrowLeft' })
    expect(onChange).toHaveBeenLastCalledWith(3)
    fireEvent.keyDown(thumb, { key: 'End' })
    expect(onChange).toHaveBeenLastCalledWith(10)
    fireEvent.keyDown(thumb, { key: 'Home' })
    expect(onChange).toHaveBeenLastCalledWith(1)
  })

  it('before the first settled round, says there are no rounds yet', () => {
    render(<TimelineBar minT={0} maxT={0} t={0} onChange={vi.fn()} markers={[]} following />)
    expect(screen.getByRole('slider', { name: 'Round' })).toHaveAttribute('aria-valuetext', 'No rounds yet')
  })

  it('with one round, shows the round and disables the slider (no zero range)', () => {
    render(<TimelineBar minT={1} maxT={1} t={1} onChange={vi.fn()} markers={[]} following />)
    const thumb = screen.getByRole('slider', { name: 'Round' })
    expect(thumb).toHaveAttribute('aria-valuetext', 'Round 1 of 1')
    expect(thumb).toHaveAttribute('data-disabled')
    expect(document.body.innerHTML).not.toContain('NaN')
  })

  it('announces the round when the viewer moves it, and stays quiet while playing', () => {
    renderBar()
    const counter = screen.getByText(/^R4/)
    expect(counter).toHaveAttribute('aria-live', 'polite')
    fireEvent.click(screen.getByRole('button', { name: 'Play replay' }))
    expect(counter).toHaveAttribute('aria-live', 'off')
  })
})

describe('theme', () => {
  it('a chosen theme sets the class and is kept for the next visit', () => {
    setTheme('dark')
    expect(document.documentElement).toHaveClass('dark')
    expect(localStorage.getItem('theme')).toBe('dark')
    setTheme('light')
    expect(document.documentElement).not.toHaveClass('dark')
  })

  it('follows the OS until the viewer chooses, then keeps the choice', () => {
    let onChange: () => void = () => {}
    const mq = { matches: false, addEventListener: (_: string, fn: () => void) => (onChange = fn), removeEventListener: () => {} }
    vi.stubGlobal('matchMedia', () => mq)
    function Follow() {
      useFollowSystemTheme()
      return null
    }
    render(<Follow />)
    mq.matches = true
    act(() => onChange())
    expect(document.documentElement).toHaveClass('dark')
    setTheme('light')
    act(() => onChange())
    expect(document.documentElement).not.toHaveClass('dark')
  })
})

describe('about', () => {
  it('shows the TradingView notice with its link, as the licence asks', () => {
    render(<AboutPage />)
    const notice = screen.getByTestId('tradingview-notice')
    expect(notice).toHaveTextContent('"TradingView Lightweight Charts™" Copyright (с) 2025 TradingView, Inc. https://www.tradingview.com/')
    expect(within(notice).getByRole('link')).toHaveAttribute('href', 'https://www.tradingview.com/')
  })
})

describe('labels', () => {
  it('names a research run with no labels as an ablation branch, not as exploratory', () => {
    render(<LabelsText labels={null} kind="research" />)
    expect(screen.getByText('None (ablation branch)')).toBeInTheDocument()
    cleanup()
    render(<LabelsText labels={null} kind="exploratory" />)
    expect(screen.getByText('None (exploratory)')).toBeInTheDocument()
  })
})
