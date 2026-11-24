// Shared API shapes and small helpers for the Phase 2 workspaces.
// These types are intentionally permissive: older backend snapshots lack some fields.

export type GuardrailArm = 'off' | 'prefilter' | 'scanners' | 'input' | 'output' | 'full' | 'adaptive'

export type GuardrailArmInfo = {
  id: GuardrailArm
  code: string
  label: string
  description: string
  requiresClassifier: boolean
  setupTitle?: string
}

export const GUARDRAIL_ARMS: GuardrailArmInfo[] = [
  { id: 'off', code: 'G0', label: 'Off', description: 'No guardrails (baseline)', requiresClassifier: false },
  { id: 'prefilter', code: 'G1', label: 'Prefilter', description: 'Heuristic pattern prefilter', requiresClassifier: false },
  { id: 'scanners', code: 'G2', label: 'Scanners', description: 'Input scanners only', requiresClassifier: false },
  {
    id: 'input',
    code: 'G3',
    label: 'Input guard',
    description: 'Input scanners + classifier',
    requiresClassifier: true,
    setupTitle: 'Input guard needs the classifier model or Cloudflare Workers AI credentials.',
  },
  {
    id: 'output',
    code: 'G4',
    label: 'Output guard',
    description: 'Output classifier checks',
    requiresClassifier: true,
    setupTitle: 'Output guard needs the classifier model or Cloudflare Workers AI credentials.',
  },
  {
    id: 'full',
    code: 'G5',
    label: 'Full guards',
    description: 'Scanners + classifier',
    requiresClassifier: true,
    setupTitle: 'Full guards need the classifier model or Cloudflare Workers AI credentials.',
  },
  {
    id: 'adaptive',
    code: 'G6',
    label: 'Adaptive',
    description: 'Classifier on review only',
    requiresClassifier: true,
    setupTitle: 'Adaptive calls the classifier on review cases; unavailable until it is configured.',
  },
]

export const ARM_IDS = GUARDRAIL_ARMS.map((arm) => arm.id)

export function armInfo(id: GuardrailArm): GuardrailArmInfo {
  return GUARDRAIL_ARMS.find((arm) => arm.id === id) ?? GUARDRAIL_ARMS[0]
}

export function armNeedsClassifier(id: GuardrailArm): boolean {
  return Boolean(armInfo(id).requiresClassifier)
}

export function armIsDisabled(id: GuardrailArm, adaptiveEnabled?: boolean): boolean {
  // The server rejects the adaptive arm outright when ADAPTIVE_ENABLED=false.
  if (id === 'adaptive' && adaptiveEnabled === false) return true
  return false
}

export function armLabel(id: GuardrailArm, configured?: boolean): string {
  const info = armInfo(id)
  if (info.requiresClassifier && configured === false) return `${info.label} · setup`
  return info.label
}

export type GuardrailFinding = {
  scanner: string
  outcome: 'passed' | 'redacted' | 'blocked'
  risk_score?: number | null
  category?: string | null
}

export type GuardrailStage = {
  stage: string
  backend: string
  outcome: 'passed' | 'redacted' | 'blocked' | 'skipped' | 'review'
  blocked_by?: string | null
  category?: string | null
  latency_ms: number
  findings: GuardrailFinding[]
}

export type GuardrailVerdict = {
  level: GuardrailArm
  outcome: 'passed' | 'redacted' | 'blocked'
  blocked: boolean
  blocked_by?: string | null
  category?: string | null
  latency_ms: number
  findings?: GuardrailFinding[]
  stages?: GuardrailStage[]
  classifier_calls?: number
  prefilter?: Record<string, unknown> | null
}

export type CostInfo = {
  priced: boolean
  notional_usd?: number | null
  marginal_usd?: number | null
  currency?: string
  billing_mode?: string
  pricing_sha256?: string | null
  unpriced_reason?: string | null
}

export type ChatUsage = {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
}

export type ChatResponse = {
  answer: string
  model: string
  latency_ms: number
  usage?: ChatUsage
  guardrail: GuardrailVerdict
  sanitized_user_message?: string | null
  cost?: CostInfo | null
  trace_id?: string | null
  fallback_used?: boolean
  fallback_from?: string | null
  attempts?: number
}

export type ModelListResponse = { data: Array<{ id: string; object?: string; owned_by?: string }> }

export type PricingModel = {
  id: string
  input_per_mtok?: number | null
  output_per_mtok?: number | null
  cached_input_per_mtok?: number | null
  free?: boolean
  priced?: boolean
  notes?: string | null
  context_limit?: number | null
  output_limit?: number | null
  limits?: { context?: number | null; output?: number | null; [key: string]: unknown }
  allowance_unit?: string | null
  allowance_limit?: number | null
  allowance_window?: string | null
  [key: string]: unknown
}

export type PricingDocument = {
  provider?: string
  schema_version?: number
  retrieved_at?: string
  source_url?: string
  provider_doc_url?: string
  billing_mode?: string
  currency?: string
  notes?: string
  sha256?: string
  pricing_sha256?: string
  models: PricingModel[] | Record<string, PricingModel>
  guardrails?: Record<string, Record<string, unknown>>
}

export type TraceSpan = {
  name: string
  start_offset_ms: number
  duration_ms: number
  attributes?: Record<string, unknown>
}

export type TraceRecord = {
  trace_id: string
  created_at: string
  kind?: string
  request_id?: string | null
  session_id?: string | null
  route?: string | null
  model?: string | null
  backend?: string | null
  arm?: GuardrailArm | string | null
  status?: string
  total_latency_ms?: number
  spans?: TraceSpan[]
  verdict?: GuardrailVerdict | null
  cost?: CostInfo | null
  usage?: ChatUsage | null
  error?: string | null
  [key: string]: unknown
}

export type ObservabilityConfig = {
  tracing_enabled: boolean
  content_capture_enabled: boolean
  phoenix_url?: string | null
  prometheus_url?: string | null
  grafana_url?: string | null
  alert_rules?: string[]
}

export type CompareVariantResult = {
  model: string
  guardrail?: GuardrailVerdict | string | null
  answer: string
  latency_ms: number
  cost?: CostInfo | null
  trace_id?: string | null
}

export type CompareResponse = { variants: CompareVariantResult[] }

export async function getJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  let body: unknown = null
  try {
    body = await response.json()
  } catch {
    // Leave body as null; error formatting below still works.
  }
  if (!response.ok) {
    const detail = typeof body === 'object' && body !== null && 'detail' in body
      ? String((body as { detail?: unknown }).detail)
      : `Request failed (${response.status})`
    throw new Error(detail)
  }
  return body as T
}

export function shortId(value: string | null | undefined, length = 8): string {
  if (!value) return '—'
  return value.length <= length ? value : `${value.slice(0, length)}…`
}

export function money(value: number | null | undefined, currency = 'USD'): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  const digits = Math.abs(value) > 0 && Math.abs(value) < 0.01 ? 6 : 4
  return `${currency === 'USD' ? '$' : `${currency} `}${value.toFixed(digits)}`
}

export function costLabel(cost: CostInfo | null | undefined): string | null {
  if (!cost) return 'unpriced'
  if (!cost.priced) return `unpriced${cost.unpriced_reason ? ` (${cost.unpriced_reason})` : ''}`
  const currency = cost.currency ?? 'USD'
  return `notional ${money(cost.notional_usd, currency)} · marginal ${money(cost.marginal_usd, currency)}`
}

export function normalisePricingModels(document: PricingDocument | null): PricingModel[] {
  if (!document?.models) return []
  const entries: PricingModel[] = Array.isArray(document.models)
    ? document.models
    : Object.entries(document.models).map(([id, value]) => ({ ...(value ?? {}), id: value?.id ?? id }))
  return [...entries].sort((a, b) => a.id.localeCompare(b.id))
}

export function findPricingModel(document: PricingDocument | null, modelId: string): PricingModel | undefined {
  const models = normalisePricingModels(document)
  return models.find((model) => model.id === modelId)
    ?? models.find((model) => model.id.toLowerCase() === modelId.toLowerCase())
}

const ESTIMATED_INPUT_TOKENS = 1000
const ESTIMATED_OUTPUT_TOKENS = 250

export function estimateNotionalUsd(
  document: PricingDocument | null,
  modelId: string,
  requests: number,
): number | null {
  const price = findPricingModel(document, modelId)
  if (!price || price.priced === false) return null
  const inputRate = price.input_per_mtok
  const outputRate = price.output_per_mtok
  if (inputRate === null && outputRate === null) return null
  if (inputRate === undefined && outputRate === undefined) return null
  const perRequest =
    (ESTIMATED_INPUT_TOKENS * (inputRate ?? 0) + ESTIMATED_OUTPUT_TOKENS * (outputRate ?? 0)) / 1_000_000
  return perRequest * Math.max(0, requests)
}

export const ESTIMATED_TOKENS_NOTE =
  `Estimate assumes ~${ESTIMATED_INPUT_TOKENS} input and ${ESTIMATED_OUTPUT_TOKENS} output tokens per request.`

export function verdictOutcome(verdict: GuardrailVerdict | string | null | undefined): string {
  if (!verdict) return '—'
  if (typeof verdict === 'string') return verdict
  const stageText = (verdict.stages ?? [])
    .filter((stage) => stage.outcome === 'blocked' || stage.outcome === 'redacted')
    .map((stage) => `${stage.stage}: ${stage.blocked_by ?? stage.outcome}`)
    .join(' · ')
  if (stageText) return stageText
  if (verdict.blocked) return `blocked · ${verdict.blocked_by ?? 'guardrail'}`
  return verdict.outcome
}
