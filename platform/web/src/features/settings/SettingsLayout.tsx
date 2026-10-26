import type { ReactNode } from 'react'
import { NavLink, Outlet } from 'react-router'
import { cn } from '@/lib/utils'

/** DECISIONS-11: every setting is made here. The files stay the storage and the audit trail; the research invariants are not settings. */
const TABS = [
  { to: 'profiles', label: 'Profiles' },
  { to: 'defaults', label: 'Defaults' },
  { to: 'conditions', label: 'Conditions' },
  { to: 'research', label: 'Research files' },
  { to: 'freeze', label: 'Freeze' },
]

export function SettingsLayout() {
  return (
    <div className="space-y-4">
      <nav aria-label="Settings" className="flex flex-wrap gap-1 border-b pb-2">
        {TABS.map((t) => (
          <NavLink key={t.to} to={t.to} className={({ isActive }) => cn('rounded-md px-2.5 py-1 text-sm', isActive ? 'bg-muted font-medium' : 'text-muted-foreground hover:text-foreground')}>
            {t.label}
          </NavLink>
        ))}
      </nav>
      <Outlet />
    </div>
  )
}

export function ErrorText({ children }: { children: string | null | undefined }) {
  if (!children) return null
  return (
    <p role="alert" className="rounded-md border border-red-500/40 bg-red-500/10 p-2 text-sm">
      {children}
    </p>
  )
}

export function PageHead({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <h1 className="text-xl font-semibold">{title}</h1>
      <p className="text-sm text-muted-foreground">{children}</p>
    </div>
  )
}
