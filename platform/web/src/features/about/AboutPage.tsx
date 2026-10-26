const LICENCES = [
  { name: 'TradingView Lightweight Charts', licence: 'Apache-2.0', url: 'https://www.tradingview.com/' },
  { name: 'React', licence: 'MIT', url: 'https://react.dev/' },
  { name: 'shadcn/ui and Radix UI', licence: 'MIT', url: 'https://ui.shadcn.com/' },
  { name: 'Magic UI', licence: 'MIT', url: 'https://magicui.design/' },
  { name: 'Motion', licence: 'MIT', url: 'https://motion.dev/' },
  { name: 'Tailwind CSS', licence: 'MIT', url: 'https://tailwindcss.com/' },
  { name: 'TanStack Query', licence: 'MIT', url: 'https://tanstack.com/query' },
  { name: 'Geist font', licence: 'SIL OFL 1.1', url: 'https://vercel.com/font' },
]

export function AboutPage() {
  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <section className="space-y-2">
        <h1 className="text-xl font-semibold">About</h1>
        <p className="text-sm text-muted-foreground">
          A local tool to run and replay market simulations for the Apart sprint, question 2.3. It shows what the backend recorded. It does not
          score collusion and it does not compute labels.
        </p>
      </section>

      <section className="space-y-2 rounded-lg border p-4">
        <h2 className="font-medium">Charts</h2>
        <p className="text-sm">
          The price charts use TradingView Lightweight Charts.
        </p>
        <p className="font-mono text-sm" data-testid="tradingview-notice">
          "TradingView Lightweight Charts™" Copyright (с) 2025 TradingView, Inc.{' '}
          <a className="underline underline-offset-4" href="https://www.tradingview.com/" target="_blank" rel="noreferrer noopener">
            https://www.tradingview.com/
          </a>
        </p>
      </section>

      <section className="space-y-2">
        <h2 className="font-medium">Open-source licences</h2>
        <ul className="divide-y rounded-lg border text-sm">
          {LICENCES.map((l) => (
            <li key={l.name} className="flex items-center justify-between gap-4 px-4 py-2">
              <a className="underline-offset-4 hover:underline" href={l.url} target="_blank" rel="noreferrer noopener">
                {l.name}
              </a>
              <span className="text-muted-foreground">{l.licence}</span>
            </li>
          ))}
        </ul>
        <p className="text-xs text-muted-foreground">The market formula follows ColludeBench (MIT). No code is copied from it.</p>
      </section>
    </div>
  )
}
