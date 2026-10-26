import { Moon, Sun } from 'lucide-react'
import { NavLink, Outlet } from 'react-router'
import { Button } from '@/components/ui/button'
import { useHealth } from '@/api/queries'
import { cn } from '@/lib/utils'
import { setTheme, useFollowSystemTheme, useTheme } from './theme'

const NAV = [
  { to: '/runs', label: 'Runs' },
  { to: '/workbench', label: 'Workbench' },
  { to: '/scenarios', label: 'Scenarios' },
  { to: '/conditions', label: 'Conditions' },
  { to: '/experiments', label: 'Experiments' },
  { to: '/evaluations', label: 'Evaluate' },
  { to: '/settings', label: 'Settings' },
  { to: '/architecture', label: 'Architecture' },
  { to: '/data', label: 'Data' },
  { to: '/files', label: 'Files' },
  { to: '/about', label: 'About' },
]

function BackendStatus() {
  const h = useHealth()
  const ok = h.data?.status === 'ok'
  const label = h.isPending ? 'Checking backend' : ok ? 'Backend OK' : h.data ? 'Backend degraded' : 'No backend'
  return (
    <span className="flex items-center gap-1.5 text-xs text-muted-foreground" title={h.data ? `API ${h.data.api_version}, simcore ${h.data.simcore_version}` : undefined}>
      <span aria-hidden className={cn('size-2 rounded-full', h.isPending ? 'bg-muted-foreground' : ok ? 'bg-emerald-500' : 'bg-red-500')} />
      <span className="hidden sm:inline">{label}</span>
      <span className="sr-only sm:hidden">{label}</span>
    </span>
  )
}

export function AppShell() {
  useFollowSystemTheme()
  const theme = useTheme()
  return (
    <div className="min-h-dvh">
      <header className="sticky top-0 z-20 border-b bg-background/95 backdrop-blur">
        <div className="mx-auto flex max-w-screen-2xl flex-wrap items-center gap-x-2 gap-y-1 px-4 py-2 sm:h-12 sm:flex-nowrap sm:gap-4 sm:py-0">
          <span className="font-semibold tracking-tight whitespace-nowrap">Market simulator</span>
          {import.meta.env.MODE === 'mock' && (
            <span className="rounded border border-dashed px-1.5 text-xs whitespace-nowrap text-muted-foreground" title="All data comes from the mock server. Do not cite it.">
              mock data
            </span>
          )}
          <nav className="order-last flex w-full flex-wrap gap-1 sm:order-none sm:w-auto sm:flex-nowrap">
            {NAV.map((n) => (
              <NavLink
                key={n.to}
                to={n.to}
                className={({ isActive }) =>
                  cn('rounded-md px-2 py-1 text-sm text-muted-foreground hover:text-foreground', isActive && 'bg-muted text-foreground')
                }
              >
                {n.label}
              </NavLink>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-2">
            <BackendStatus />
            <Button
              variant="ghost"
              size="icon"
              aria-label={theme === 'dark' ? 'Use light theme' : 'Use dark theme'}
              onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
            >
              {theme === 'dark' ? <Sun /> : <Moon />}
            </Button>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-screen-2xl px-4 py-4">
        <Outlet />
      </main>
    </div>
  )
}
