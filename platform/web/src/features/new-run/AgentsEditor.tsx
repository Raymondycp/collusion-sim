import { useState, type ReactNode } from 'react'
import { Eye, Plus, Trash2 } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import type { AgentConfig, Capability, Condition, RunConfig } from '@/api/runConfig'
import { previewPrompt } from '@/api/runConfig'
import { useProfileModels } from '@/api/providers'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { resolve, type JSONSchema, type Path } from '@/lib/jsonSchema'
import { readCallsText } from '@/features/conditions/limits'
import { addAgent, removeAgent, defaultGrant, defaultLlm, missingDefaultProfile, policiesForRole, policyParams, pullBlind, pullWithoutReads, switched, toolsForRole, type Registries } from './draft'
import { FieldError, FieldShell, fieldId, LockBadge, NativeSelect, SchemaField, useForm, useHasErrorUnder } from './SchemaField'

function PromptPreviewDialog({ config, sellerId, onClose }: { config: RunConfig; sellerId: string; onClose: () => void }) {
  const q = useQuery({ queryKey: ['prompt-preview', sellerId, JSON.stringify(config)], queryFn: () => previewPrompt(config, sellerId), retry: false, gcTime: 0 })
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Prompt for {sellerId}, round 1</DialogTitle>
          <DialogDescription>The exact messages and tools that the backend will send. Built without a model call; rounds that have not run use placeholder data.</DialogDescription>
        </DialogHeader>
        {q.isPending && <p className="text-sm text-muted-foreground">Building the request…</p>}
        {q.error && <p role="alert" className="text-sm text-red-600 dark:text-red-400">{q.error.message}</p>}
        {q.data && (
          <div className="space-y-3 text-sm">
            {q.data.messages.map((m, i) => (
              <div key={i}>
                <p className="text-xs font-semibold text-muted-foreground uppercase">{m.role}</p>
                <pre className="rounded-md bg-muted/50 p-2 text-xs whitespace-pre-wrap">{m.content}</pre>
              </div>
            ))}
            <details>
              <summary className="cursor-pointer text-sm">Tools sent ({q.data.tools.length})</summary>
              <pre className="mt-1 rounded-md bg-muted/50 p-2 text-xs whitespace-pre-wrap">{JSON.stringify(q.data.tools, null, 2)}</pre>
            </details>
            <p className="font-mono text-xs text-muted-foreground">
              {q.data.request_hash}
              {q.data.placeholder_fields?.length ? `. Placeholders: ${q.data.placeholder_fields.join(', ')}` : ''}
            </p>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

function LockedGrants({ cap, limits }: { cap: Capability | null | undefined; limits: Condition['limits'] }) {
  const params = (p: Record<string, unknown> | undefined) =>
    Object.entries(p ?? {})
      .map(([k, v]) => `${k} ${Array.isArray(v) ? v.join(', ') : String(v)}`)
      .join('; ')
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-1.5 text-sm font-medium">
        Tools
        <LockBadge />
      </div>
      <ul className="rounded-md bg-muted/50 px-2.5 py-1.5 text-xs">
        {cap?.grants.map((g) => (
          <li key={g.tool_id}>
            <span className="font-mono">{g.tool_id}</span>
            {params(g.params) && <span className="text-muted-foreground"> ({params(g.params)})</span>}
          </li>
        )) ?? <li>none</li>}
        {cap && <li className="text-muted-foreground">{readCallsText(limits, cap)}</li>}
      </ul>
    </div>
  )
}

/** A grant that a run switch turns off shows as off, and keeps its place in the request, so it comes back when the switch is on. */
function ToolPicker({ path, agent, reg, request }: { path: Path; agent: AgentConfig; reg: Registries; request?: RunConfig }) {
  const { set, root } = useForm()
  const tools = toolsForRole(agent.role, reg)
  const cap: Capability = agent.capability ?? { grants: [], max_queries_per_round: 3 }
  const capSchema = resolve(root, root.$defs?.Capability ?? {})
  const hasError = useHasErrorUnder([...path, 'capability'])
  if (tools.length === 0) return <p className="text-sm text-muted-foreground">This role has no tools.</p>
  const state = (t: (typeof tools)[number]) => switched(t, cap.grants.find((g) => g.tool_id === t.id), request)
  const on = tools.filter((t) => cap.grants.some((g) => g.tool_id === t.id) && !state(t).off).length
  const blind = pullWithoutReads(agent, request) ?? pullBlind(agent, request, tools)
  return (
    <details className="rounded-md border p-2" open={hasError || undefined}>
      <summary className="cursor-pointer text-xs font-medium">
        Tools ({on} of {tools.length} granted)
      </summary>
      <div className="mt-1.5 space-y-1.5">
      <p className="text-[11px] text-muted-foreground">Ungranted tools are not offered. Read tools follow the agent view.</p>
      {blind && <p role="status" className="text-xs text-amber-700 dark:text-amber-400">{blind}</p>}
      <ul className="space-y-2">
        {tools.map((t) => {
          const j = cap.grants.findIndex((g) => g.tool_id === t.id)
          const granted = j !== -1
          const { off, note } = state(t)
          const paramsSchema = t.params_schema as JSONSchema
          const hasParams = Object.keys(paramsSchema.properties ?? {}).length > 0
          return (
            <li key={t.id} className="rounded-md border p-2">
              <label className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  className="mt-0.5 size-4"
                  checked={granted && !off}
                  disabled={!!off}
                  onChange={(e) =>
                    set([...path, 'capability'], { ...cap, grants: e.target.checked ? [...cap.grants, defaultGrant(t, root)] : cap.grants.filter((g) => g.tool_id !== t.id) })
                  }
                />
                <span className="min-w-0">
                  <span className="font-mono">{t.id}</span>{' '}
                  <span className="text-xs text-muted-foreground" title={t.data_layer === 'A' ? 'Layer A: a feature that reads this tool needs only the log.' : 'Layer B: a feature that reads this tool also needs the model calls or the branch runs.'}>
                    {t.kind}, {t.data_layer === 'A' ? 'log only (layer A)' : `layer ${t.data_layer}`}
                    {t.counts_as_query ? ', counts as a query' : ''}
                  </span>
                  <span className="block text-xs text-muted-foreground">{t.description}</span>
                  {off && <span className="block text-xs text-amber-700 dark:text-amber-400">{off}</span>}
                  {granted && note && <span className="block text-xs text-amber-700 dark:text-amber-400">{note}</span>}
                </span>
              </label>
              {granted && !off && hasParams && (
                <div className="mt-2 pl-6">
                  <SchemaField path={[...path, 'capability', 'grants', j, 'params']} name="params" schema={{ ...paramsSchema, title: 'Grant settings' }} value={cap.grants[j]!.params ?? {}} />
                </div>
              )}
              <FieldError path={[...path, 'capability', 'grants', j]} />
            </li>
          )
        })}
      </ul>
      {capSchema.properties?.max_queries_per_round && (
        <SchemaField path={[...path, 'capability', 'max_queries_per_round']} name="max_queries_per_round" schema={capSchema.properties.max_queries_per_round} value={cap.max_queries_per_round} required />
      )}
      </div>
    </details>
  )
}

function LlmFields({ path, agent, reg }: { path: Path; agent: AgentConfig; reg: Registries }) {
  const { set, root, research } = useForm()
  const missing = research ? null : missingDefaultProfile(reg, agent.id)
  const llm = (agent.policy.llm ?? {}) as Record<string, unknown>
  const profileId = typeof llm.provider_profile_id === 'string' ? llm.provider_profile_id : ''
  const model = typeof llm.model === 'string' ? llm.model : ''
  const models = useProfileModels(profileId || null)
  const llmPath = [...path, 'policy', 'llm']
  const schema = resolve(root, root.$defs?.LlmSettings ?? {})
  const profile = reg.profiles.find((p) => p.id === profileId)
  const hasError = useHasErrorUnder(llmPath)
  // One selectable model per keyed profile that declares a default (OpenCode Go: Luna / Haiku / DeepSeek).
  const modelChoices = reg.profiles
    .filter((p) => p.key_set && p.default_model)
    .map((p) => ({ model: p.default_model as string, profileId: p.id, label: `${p.default_model} · ${p.name}` }))
  const known = new Set(modelChoices.map((c) => c.model))
  const pickModel = (next: string) => {
    const choice = modelChoices.find((c) => c.model === next)
    if (choice) set(llmPath, { ...llm, model: choice.model, provider_profile_id: choice.profileId })
    else set(llmPath, { ...llm, model: next })
  }
  return (
    <fieldset className="space-y-2 rounded-md border p-2">
      <legend className="px-1 text-xs font-medium">LLM</legend>
      <div className="space-y-1.5">
        <FieldShell path={[...llmPath, 'model']} label="Model">
          <NativeSelect id={fieldId([...llmPath, 'model'])} value={known.has(model) ? model : model ? '__custom__' : ''} onChange={(e) => e.target.value !== '__custom__' && pickModel(e.target.value)}>
            <option value="">Select…</option>
            {modelChoices.map((c) => (
              <option key={`${c.profileId}:${c.model}`} value={c.model}>
                {c.label}
              </option>
            ))}
            {model && !known.has(model) && <option value="__custom__">{model} (custom)</option>}
          </NativeSelect>
          {(!known.has(model) || modelChoices.length === 0) && (
            <Input className="mt-1" list={`models-${agent.id}`} value={model} onChange={(e) => pickModel(e.target.value)} placeholder="Or type a model id" />
          )}
          <datalist id={`models-${agent.id}`}>
            {models.data?.models.map((m) => <option key={m} value={m} />)}
          </datalist>
        </FieldShell>
        <FieldShell path={[...llmPath, 'provider_profile_id']} label="Profile">
          <NativeSelect
            id={fieldId([...llmPath, 'provider_profile_id'])}
            value={profileId}
            onChange={(e) => {
              const p = reg.profiles.find((x) => x.id === e.target.value)
              set(llmPath, { ...llm, provider_profile_id: e.target.value, model: p?.default_model ?? llm.model ?? '' })
            }}
          >
            <option value="">Select…</option>
            {reg.profiles.map((p) => (
              <option key={p.id} value={p.id} disabled={!p.key_set}>
                {p.name}
                {p.key_set ? '' : ' (no key)'}
              </option>
            ))}
          </NativeSelect>
          {missing && <p className="text-xs text-amber-700 dark:text-amber-400">Default profile {missing} not found on this server, so its default model is not used.</p>}
          {profile && !profile.pricing && (
            <p className="truncate text-xs text-amber-700 dark:text-amber-400" title="This profile has no price, so the cost of its calls is not counted. Set a call or token cap in the budget.">
              No price on this profile — set a budget cap.
            </p>
          )}
        </FieldShell>
      </div>
      <details open={hasError || undefined}>
        <summary className="cursor-pointer text-sm">More LLM settings</summary>
        <div className="mt-2 space-y-3">
          {Object.entries(schema.properties ?? {})
            .filter(([k]) => k !== 'provider_profile_id' && k !== 'model')
            .map(([k, p]) => (
              <SchemaField key={k} path={[...llmPath, k]} name={k} schema={p} value={llm[k]} required={(schema.required ?? []).includes(k)} />
            ))}
        </div>
      </details>
      <FieldError path={llmPath} />
    </fieldset>
  )
}

function AgentCard({ path, agent, request, reg, condition, onRemove, removeBlocked, children }: { path: Path; agent: AgentConfig; request?: RunConfig; reg: Registries; condition?: Condition; onRemove?: () => void; removeBlocked?: string; children?: ReactNode }) {
  const { set, research } = useForm()
  const [preview, setPreview] = useState(false)
  const policies = policiesForRole(agent.role, research, reg)
  const policy = reg.policies.find((p) => p.kind === agent.policy.kind)
  const paramsSchema: JSONSchema = {
    type: 'object',
    title: 'Policy settings',
    properties: Object.fromEntries((policy?.params ?? []).map((p) => [p.name, { type: p.type, default: p.default } as JSONSchema])),
  }
  return (
    <li className="space-y-2 rounded-md border p-2">
      <div className="flex flex-wrap items-end gap-1.5">
        <div className="min-w-[10rem] flex-1 basis-[10rem]">
          <FieldShell path={[...path, 'id']} label="Id">
            <Input id={fieldId([...path, 'id'])} value={agent.id} readOnly={research} onChange={(e) => set([...path, 'id'], e.target.value)} className="h-8 font-mono" />
          </FieldShell>
        </div>
        <div className="grid min-w-[10rem] flex-1 basis-[10rem] grid-cols-[6.5rem_minmax(0,1fr)] items-center gap-x-2">
          <span className="text-xs font-medium leading-none text-muted-foreground">Role</span>
          <span className="inline-block rounded-md border px-2 py-1 font-mono text-sm">{agent.role}</span>
        </div>
        <div className="min-w-[10rem] flex-1 basis-[10rem]">
          <FieldShell path={[...path, 'policy', 'kind']} label="Policy" help={policy && policy.name !== policy.kind ? policy.name : undefined}>
            <NativeSelect
              id={fieldId([...path, 'policy', 'kind'])}
              value={agent.policy.kind}
              onChange={(e) => {
                const p = reg.policies.find((x) => x.kind === e.target.value)
                set([...path, 'policy'], p?.kind === 'llm' ? { kind: 'llm', llm: defaultLlm(reg, research ? undefined : agent.id) } : { kind: p?.kind, params: policyParams(p) })
              }}
            >
              {policies.map((p) => (
                <option key={p.kind} value={p.kind}>
                  {p.kind}
                </option>
              ))}
            </NativeSelect>
          </FieldShell>
        </div>
        <div className="flex gap-1">
          {request && agent.policy.kind === 'llm' && agent.role === 'seller' && (
            <Button type="button" variant="outline" size="sm" onClick={() => setPreview(true)}>
              <Eye /> Preview prompt
            </Button>
          )}
          {onRemove && (
            <Button type="button" variant="ghost" size="icon" className="size-8" aria-label={`Remove agent ${agent.id}`} title={removeBlocked} disabled={!!removeBlocked} onClick={onRemove}>
              <Trash2 />
            </Button>
          )}
        </div>
      </div>
      {agent.policy.kind === 'llm' ? (
        <LlmFields path={path} agent={agent} reg={reg} />
      ) : (
        Object.keys(paramsSchema.properties!).length > 0 && <SchemaField path={[...path, 'policy', 'params']} name="params" schema={paramsSchema} value={agent.policy.params ?? {}} />
      )}
      {research ? (
        agent.role === 'seller' && (condition ? <LockedGrants cap={condition.capabilities[agent.id]} limits={condition.limits} /> : <p className="text-xs text-muted-foreground">Tools: set by each condition.</p>)
      ) : (
        <ToolPicker path={path} agent={agent} reg={reg} request={request} />
      )}
      {children}
      {preview && request && <PromptPreviewDialog config={request} sellerId={agent.id} onClose={() => setPreview(false)} />}
    </li>
  )
}

/** The server refuses a run with fewer sellers. The page says so early; the server still decides. */
const MIN_SELLERS = 2
const MIN_SELLERS_TEXT = `A run needs at least ${MIN_SELLERS} sellers. The server refuses fewer.`

/**
 * The agents at `path`. `request` is the run config as it will be sent (locked fields cleared); the prompt preview needs
 * it, so the preview is offered only with one. Without a condition, a research agent's tools show as set per condition.
 * `grid` is the workbench setup layout (UI-WORKBENCH.md §2): seller cards side by side, a `+` card that adds a seller,
 * and a Host card that holds the host agent and `hostNews`.
 */
export function AgentsEditor({ agents, path = ['agents'], request, reg, condition, grid, hostNews }: { agents: AgentConfig[]; path?: Path; request?: RunConfig; reg: Registries; condition?: Condition; grid?: boolean; hostNews?: ReactNode }) {
  const { set, research } = useForm()
  const [role, setRole] = useState('seller')
  const runRoles = reg.roles.filter((r) => r.stage === 'run')
  const hasHost = agents.some((a) => a.role === 'host')
  const allowed = (r: (typeof runRoles)[number]) => r.built && !(r.name === 'host' && hasHost)
  const sellers = agents.filter((a) => a.role === 'seller').length
  const add = (r: string) => set(path, addAgent(r, agents, reg))
  const card = (a: AgentConfig, i: number, children?: ReactNode) => (
    <AgentCard
      key={i}
      path={[...path, i]}
      agent={a}
      request={request}
      reg={reg}
      condition={condition}
      onRemove={research ? undefined : () => set(path, removeAgent(agents, i))}
      removeBlocked={a.role === 'seller' && sellers <= MIN_SELLERS ? MIN_SELLERS_TEXT : undefined}
    >
      {children}
    </AgentCard>
  )
  const tooFew = sellers < MIN_SELLERS && (
    <p role="status" className="text-sm text-amber-700 dark:text-amber-400">
      {MIN_SELLERS_TEXT}
    </p>
  )

  if (grid) {
    const host = agents.findIndex((a) => a.role === 'host')
    return (
      <div className="space-y-2">
        <ul className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
          {agents.map((a, i) => (a.role === 'host' ? null : card(a, i)))}
          {!research && (
            <li>
              <button
                type="button"
                className="flex h-full min-h-24 w-full items-center justify-center gap-1.5 rounded-lg border border-dashed text-sm text-muted-foreground hover:bg-muted/50"
                onClick={() => add('seller')}
              >
                <Plus className="size-4" /> Add seller
              </button>
            </li>
          )}
        </ul>
        {tooFew}
        <FieldError path={path} />
        <section aria-label="Host" className="space-y-2">
          <h3 className="text-sm font-medium">Host</h3>
          {host !== -1 ? (
            <ul>{card(agents[host]!, host, hostNews)}</ul>
          ) : (
            <div className="space-y-2 rounded-md border p-2">
              <p className="text-xs text-muted-foreground">No host agent: nobody posts the news.</p>
              {!research && runRoles.some((r) => r.name === 'host' && r.built) && (
                <Button type="button" variant="outline" size="sm" onClick={() => add('host')}>
                  <Plus /> Add host
                </Button>
              )}
              {hostNews}
            </div>
          )}
        </section>
        {research && <p className="text-xs text-muted-foreground">The condition sets the sellers. Research runs use the scripted host.</p>}
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <ul className="space-y-3">{agents.map((a, i) => card(a, i))}</ul>
      {tooFew}
      <FieldError path={path} />
      {research ? (
        <p className="text-xs text-muted-foreground">The condition sets the sellers. Research runs use the scripted host.</p>
      ) : (
        <div className="flex flex-wrap items-end gap-2">
          <div className="space-y-1">
            <label htmlFor="add-agent-role" className="text-sm font-medium">
              Role of the new agent
            </label>
            <NativeSelect id="add-agent-role" value={role} onChange={(e) => setRole(e.target.value)} className="w-56">
              {runRoles.map((r) => (
                <option key={r.name} value={r.name} disabled={!allowed(r)}>
                  {r.name}
                  {!r.built ? ' (designed, not built)' : r.name === 'host' && hasHost ? ' (one host only)' : ''}
                </option>
              ))}
            </NativeSelect>
          </div>
          <Button
            type="button"
            variant="outline"
            disabled={!runRoles.some((r) => r.name === role && allowed(r))}
            onClick={() => {
              add(role)
              setRole('seller')
            }}
          >
            <Plus /> Add agent
          </Button>
        </div>
      )}
    </div>
  )
}
