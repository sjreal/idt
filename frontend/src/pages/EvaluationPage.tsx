import { useCallback, useEffect, useMemo, useState } from 'react'
import { Activity, ArrowLeft, Download, ExternalLink, FileJson, FlaskConical, LoaderCircle, Play } from 'lucide-react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { Button } from '../components/ui/button'
import {
  GUARDRAIL_ARMS,
  armInfo,
  armLabel,
  armIsDisabled,
  armNeedsClassifier,
  estimateNotionalUsd,
  getJson,
  money,
  shortId,
  ESTIMATED_TOKENS_NOTE,
  type GuardrailArm,
  type PricingDocument,
} from '../lib/phase2'
import './EvaluationPage.css'
import './Phase2Pages.css'

type Arm = GuardrailArm
type GuardrailBackend = 'cloudflare' | 'ollama'

type Health = {
  llm_connected: boolean
  guardrail_classifier_backend: GuardrailBackend
  guardrail_classifier_configured: boolean
  adaptive_enabled?: boolean
}

type CI = { low: number; high: number }

type CategoryMetric = {
  count: number
  successes: number
  rate: number
  ci95?: CI | null
  wilson95?: CI | null
  exploratory?: boolean
}

type PairedStats = {
  attack_cases?: number
  benign_cases?: number
  attack_success_delta?: number | null
  attack_success_delta_ci95?: CI | null
  false_refusal_delta?: number | null
  false_refusal_delta_ci95?: CI | null
  attack_success_mcnemar?: { p_value?: number | null; pairs?: number } | null
  false_refusal_mcnemar?: { p_value?: number | null; pairs?: number } | null
  attack_success_p_holm?: number | null
  attack_success_significant_holm?: boolean
  false_refusal_p_holm?: number | null
  false_refusal_significant_holm?: boolean
  p_vs_off?: number | null
  p_holm?: number | null
  significant_holm?: boolean
}

type ArmSummary = {
  total: number
  completed: number
  errors: number
  attack_count: number
  attack_successes: number
  attack_success_rate: number | null
  attack_success_ci95: CI | null
  attack_success_wilson95?: CI | null
  attack_mde_pp?: number | null
  mde_pp?: number | null
  replicate_sd?: number | null
  replicate_stddev?: number | null
  p_vs_off?: number | null
  p_holm?: number | null
  significant_holm?: boolean
  attack_by_category: Record<string, CategoryMetric>
  benign_count: number
  false_refusals: number
  false_refusal_rate: number | null
  false_refusal_ci95: CI | null
  false_refusal_wilson95?: CI | null
  wilson95?: CI | null
  benign_task_success_rate: number | null
  guardrail_blocked: number
  p50_latency_ms: number | null
  p95_latency_ms: number | null
  p50_guardrail_latency_ms: number | null
  paired_vs_off?: PairedStats
}

type Result = {
  case_id: string
  kind: 'attack' | 'benign'
  category: string
  prompt: string
  arm: Arm
  guardrail_outcome: string | null
  guardrail_blocked: boolean
  blocked_by: string | null
  guardrail_latency_ms: number | null
  latency_ms: number | null
  attack_success: boolean | null
  false_refusal: boolean | null
  benign_task_success: boolean | null
  model_refusal_heuristic: boolean | null
  answer: string | null
  error: string | null
  trace_id?: string | null
  replicate?: number
}

type Run = {
  id: string
  status: 'queued' | 'running' | 'completed' | 'completed_with_errors' | 'failed' | 'aborted_budget'
  created_at: string
  completed_at: string | null
  arms: Arm[]
  seed: number
  dataset_version: string
  dataset_sha256: string
  sample_count: number
  generation_backend: string
  generation_model: string
  guardrail_backend: string
  results_written: number
  error: string | null
  partial?: boolean
  replicates?: number
  max_requests?: number
  hard_max_requests?: number
  planned_requests?: number
  summary: Partial<Record<Arm, ArmSummary>>
  results?: Result[]
}

type Overview = {
  version: string
  sha256: string
  provenance: string
  total: number
  attacks: number
  benign: number
  arms: Arm[]
  results_available: boolean
  runs: Run[]
}

type RunStartResponse = {
  id: string
  max_requests?: number
  hard_max_requests?: number
  planned_requests?: number
  partial?: boolean
}

type CompareRun = {
  id: string
  status: string
  partial?: boolean
  generation_model?: string
  results_written?: number
  planned_requests?: number
  summary?: Partial<Record<Arm, ArmSummary>>
}

type Comparison = {
  runs: CompareRun[]
  excluded_partial_runs?: CompareRun[]
}

type BudgetInfo = { maxRequests?: number; hardMaxRequests?: number; plannedRequests?: number }

type EvaluationPageProps = {
  health: Health | null
  generationModel: string
  availableModels: string[]
  onGenerationModelChange: (model: string) => void
  onBackToChat: () => void
  onOpenTrace?: (traceId: string) => void
}

const arms = GUARDRAIL_ARMS.map((info) => ({
  id: info.id,
  title: info.label,
  code: info.code,
  description: info.description,
  requiresClassifier: info.requiresClassifier,
  setupTitle: info.setupTitle,
}))

const armName: Record<Arm, string> = Object.fromEntries(
  GUARDRAIL_ARMS.map((info) => [info.id, `${info.code} · ${info.label}`]),
) as Record<Arm, string>

function percent(value: number | null | undefined) {
  return value === null || value === undefined ? '—' : `${(value * 100).toFixed(1)}%`
}

function pValue(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return value < 0.001 ? '<0.001' : value.toFixed(3)
}

function points(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `${value.toFixed(1)} pp`
}

function ciLabel(interval: CI | null | undefined) {
  return interval ? `${percent(interval.low)} – ${percent(interval.high)}` : '—'
}

function percentagePointDelta(value: number | null | undefined) {
  if (value === null || value === undefined) return '—'
  const points = value * 100
  return `${points > 0 ? '+' : ''}${points.toFixed(1)} pp`
}

function formatDate(value: string | null | undefined) {
  if (!value) return '—'
  return new Date(value).toLocaleString()
}

function wilsonInterval(summary: ArmSummary | undefined, kind: 'attack' | 'benign'): CI | null {
  if (!summary) return null
  if (kind === 'attack') {
    return summary.attack_success_wilson95 ?? summary.wilson95 ?? summary.attack_success_ci95 ?? null
  }
  return summary.false_refusal_wilson95 ?? summary.false_refusal_ci95 ?? null
}

function mcnemarP(summary: ArmSummary | undefined): number | null {
  const paired = summary?.paired_vs_off
  return paired?.attack_success_mcnemar?.p_value ?? paired?.p_vs_off ?? summary?.p_vs_off ?? null
}

function holmP(summary: ArmSummary | undefined): number | null {
  const paired = summary?.paired_vs_off
  return paired?.attack_success_p_holm ?? paired?.p_holm ?? summary?.p_holm ?? null
}

function holmSignificant(summary: ArmSummary | undefined): boolean | null {
  const paired = summary?.paired_vs_off
  return paired?.attack_success_significant_holm ?? paired?.significant_holm ?? summary?.significant_holm ?? null
}

function mdeValue(summary: ArmSummary | undefined): number | null {
  return summary?.attack_mde_pp ?? summary?.mde_pp ?? null
}

function replicateSdValue(summary: ArmSummary | undefined): number | null {
  return summary?.replicate_sd ?? summary?.replicate_stddev ?? null
}

function MetricCard({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <div className="eval-metric-card">
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  )
}

export function EvaluationPage({
  health,
  generationModel,
  availableModels,
  onGenerationModelChange,
  onBackToChat,
  onOpenTrace,
}: EvaluationPageProps) {
  const [overview, setOverview] = useState<Overview | null>(null)
  const [selectedRunId, setSelectedRunId] = useState('')
  const [selectedRun, setSelectedRun] = useState<Run | null>(null)
  const [selectedResult, setSelectedResult] = useState<Result | null>(null)
  const [selectedArms, setSelectedArms] = useState<Arm[]>(['off', 'scanners', 'full'])
  const [sampleLimit, setSampleLimit] = useState<number | null>(null)
  const [seed, setSeed] = useState(42)
  const [replicates, setReplicates] = useState(1)
  const [budget, setBudget] = useState(60)
  const [budgetConfirmation, setBudgetConfirmation] = useState('')
  const [budgetInfo, setBudgetInfo] = useState<BudgetInfo | null>(null)
  const [pricing, setPricing] = useState<PricingDocument | null>(null)
  const [compareRunIds, setCompareRunIds] = useState<string[]>([])
  const [comparison, setComparison] = useState<Comparison | null>(null)
  const [comparisonError, setComparisonError] = useState<string | null>(null)
  const [comparisonLoading, setComparisonLoading] = useState(false)
  const [launching, setLaunching] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const partialRunIds = (overview?.runs ?? [])
    .filter((run) => run.partial || run.status === 'aborted_budget')
    .map((run) => run.id)

  const refresh = useCallback(async () => {
    try {
      const nextOverview = await getJson<Overview>('/api/evaluations/summary')
      setOverview(nextOverview)
      const activeId = selectedRunId || nextOverview.runs[0]?.id || ''
      if (!selectedRunId && activeId) setSelectedRunId(activeId)
      if (activeId) {
        const run = await getJson<Run>(`/api/evaluations/runs/${activeId}`)
        setSelectedRun(run)
      } else {
        setSelectedRun(null)
      }
      setError(null)
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : 'Could not load evaluation data.')
    }
  }, [selectedRunId])

  useEffect(() => {
    const initialLoad = window.setTimeout(() => void refresh(), 0)
    const timer = window.setInterval(() => void refresh(), 4000)
    return () => {
      window.clearTimeout(initialLoad)
      window.clearInterval(timer)
    }
  }, [refresh])

  useEffect(() => {
    void getJson<PricingDocument>('/api/pricing')
      .then(setPricing)
      .catch(() => setPricing(null))
  }, [])

  useEffect(() => {
    if (compareRunIds.length < 2) {
      setComparison(null)
      setComparisonError(null)
      return
    }
    let active = true
    setComparisonLoading(true)
    const query = compareRunIds.map((id) => `run_ids=${encodeURIComponent(id)}`).join('&')
    // Partial runs are excluded from headline comparisons by default, so ask for
    // them explicitly whenever the user ticked one in the UI.
    const includePartial = compareRunIds.some((id) => partialRunIds.includes(id))
    const suffix = includePartial ? '&include_partial=true' : ''
    void getJson<Comparison>(`/api/evaluations/compare?${query}${suffix}`)
      .then((result) => { if (active) { setComparison(result); setComparisonError(null) } })
      .catch((loadError) => {
        if (active) setComparisonError(loadError instanceof Error ? loadError.message : 'Could not compare runs.')
      })
      .finally(() => { if (active) setComparisonLoading(false) })
    return () => { active = false }
  }, [compareRunIds, partialRunIds])

  const availableArms = selectedArms.filter(
    (arm) =>
      !armIsDisabled(arm, health?.adaptive_enabled) &&
      (!armNeedsClassifier(arm) || health?.guardrail_classifier_configured),
  )
  const effectiveSampleLimit = sampleLimit ?? overview?.total ?? 24
  const plannedRequests = effectiveSampleLimit * availableArms.length * replicates
  const estimatedCost = estimateNotionalUsd(pricing, generationModel, plannedRequests)
  const budgetReady = budget <= 60 || budgetConfirmation.trim() === 'RAISE BUDGET'
  const budgetTooSmall = plannedRequests > budget
  const compareQuery = compareRunIds.map((id) => `run_ids=${encodeURIComponent(id)}`).join('&')
  const comparePartialSuffix = compareRunIds.some((id) => partialRunIds.includes(id))
    ? '&include_partial=true'
    : ''

  const chartData = useMemo(() => {
    if (!selectedRun?.summary) return []
    return arms
      .filter(({ id }) => selectedRun.summary[id])
      .map(({ id, code }) => {
        const result = selectedRun.summary[id]
        return {
          arm: code,
          'Attack success': result?.attack_success_rate === null ? null : (result?.attack_success_rate ?? 0) * 100,
          'Benign false refusal': result?.false_refusal_rate === null ? null : (result?.false_refusal_rate ?? 0) * 100,
          'Benign task success': result?.benign_task_success_rate === null ? null : (result?.benign_task_success_rate ?? 0) * 100,
          'p95 latency (s)': result?.p95_latency_ms === null ? null : (result?.p95_latency_ms ?? 0) / 1000,
        }
      })
  }, [selectedRun])

  const setArm = (arm: Arm, checked: boolean) => {
    setSelectedArms((current) => {
      if (checked) return arms.map((entry) => entry.id).filter((item) => current.includes(item) || item === arm)
      return current.filter((item) => item !== arm)
    })
  }

  const toggleCompareRun = (id: string) => {
    setCompareRunIds((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id])
  }

  const runEvaluation = async () => {
    if (!overview || availableArms.length === 0 || !generationModel || launching || !budgetReady || budgetTooSmall) return
    const confirmText = `This sends up to ${plannedRequests} generation requests across ${availableArms.length} arm(s), with ${replicates} replicate(s) and a ${budget}-request cap. Estimated notional cost: ${estimatedCost === null ? 'unpriced' : money(estimatedCost)}. Start?`
    if (!window.confirm(confirmText)) return

    setLaunching(true)
    setError(null)
    try {
      const response = await fetch('/api/evaluations/runs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          arms: availableArms,
          sample_limit: effectiveSampleLimit,
          seed,
          replicates,
          generation_model: generationModel,
          max_requests: budget,
        }),
      })
      const body = (await response.json()) as RunStartResponse & { detail?: string }
      if (!response.ok) {
        throw new Error(typeof body.detail === 'string' ? body.detail : `Could not start run (${response.status})`)
      }
      setBudgetInfo({
        maxRequests: body.max_requests ?? budget,
        hardMaxRequests: body.hard_max_requests,
        plannedRequests: body.planned_requests ?? plannedRequests,
      })
      setSelectedRunId(body.id)
      setSelectedResult(null)
      await refresh()
    } catch (runError) {
      setError(runError instanceof Error ? runError.message : 'Could not start the evaluation.')
    } finally {
      setLaunching(false)
    }
  }

  const runActive = selectedRun?.status === 'queued' || selectedRun?.status === 'running'
  const totalResults = selectedRun
    ? (selectedRun.planned_requests ?? selectedRun.sample_count * selectedRun.arms.length * (selectedRun.replicates ?? 1))
    : 0
  const progress = selectedRun && totalResults > 0 ? Math.round((selectedRun.results_written / totalResults) * 100) : 0
  const displayedResults = selectedRun?.results?.slice(-12).reverse() ?? []
  const selectedRunSummary = selectedRun?.summary ? Object.values(selectedRun.summary) : []
  const attackCategories = [...new Set(selectedRunSummary.flatMap((item) => Object.keys(item.attack_by_category ?? {})))].sort()
  const allAttacks = selectedRunSummary.reduce((sum, item) => sum + item.attack_count, 0)
  const attackSuccesses = selectedRunSummary.reduce((sum, item) => sum + item.attack_successes, 0)
  const allBenign = selectedRunSummary.reduce((sum, item) => sum + item.benign_count, 0)
  const falseRefusals = selectedRunSummary.reduce((sum, item) => sum + item.false_refusals, 0)
  const medianGuardMs = selectedRunSummary
    .map((item) => item.p50_guardrail_latency_ms)
    .filter((item): item is number => item !== null)
  const completedRuns = (overview?.runs ?? []).filter(
    (run) => !run.partial && run.status !== 'aborted_budget'
      && (run.status === 'completed' || run.status === 'completed_with_errors'),
  )
  const partialRuns = (overview?.runs ?? []).filter((run) => partialRunIds.includes(run.id))

  const renderTraceLink = (traceId: string | null | undefined) => {
    if (!traceId) return '—'
    if (onOpenTrace) {
      return (
        <button className="trace-link" onClick={() => onOpenTrace(traceId)} title={`Open trace ${traceId}`}>
          {shortId(traceId)}
        </button>
      )
    }
    return (
      <a className="trace-link" href={`/api/traces/${encodeURIComponent(traceId)}`} target="_blank" rel="noreferrer">
        {shortId(traceId)}
      </a>
    )
  }

  return (
    <div className="evaluation-page">
      <header className="topbar evaluation-topbar">
        <div className="breadcrumb"><span>Workspace</span><span className="breadcrumb-divider">/</span><strong>Security evaluation</strong></div>
        <Button variant="outline" size="sm" onClick={onBackToChat}>
          <ArrowLeft size={14} /> Chat playground
        </Button>
      </header>

      <div className="evaluation-scroll">
        <div className="evaluation-content">
          <section className="evaluation-heading">
            <div>
              <div className="eyebrow">REPRODUCIBLE SECURITY STUDY</div>
              <h1>Guardrail evaluation</h1>
              <p>Compare attack resistance, benign false refusals, and response latency across guardrail arms.</p>
            </div>
            <div className="dataset-chip"><span className="status-dot online" /> Dataset {overview?.version ?? '—'}</div>
          </section>

          {error && <div className="evaluation-error" role="alert">{error}</div>}

          <section className="evaluation-overview-grid">
            <MetricCard label="ATTACK CASES" value={overview ? String(overview.attacks) : '—'} detail="fixed canary-leak prompts" />
            <MetricCard label="BENIGN CASES" value={overview ? String(overview.benign) : '—'} detail="ordinary task prompts" />
            <MetricCard label="DATASET CHECKSUM" value={overview?.sha256.slice(0, 12) ?? '—'} detail="SHA-256 · v1 snapshot" />
            <MetricCard label="CLASSIFIER" value={health?.guardrail_classifier_backend === 'cloudflare' ? 'Cloudflare AI' : 'Ollama'} detail={health?.guardrail_classifier_configured ? 'configured' : 'credentials/model needed'} />
          </section>

          <section className="evaluation-run-card">
            <div className="evaluation-section-title">
              <div className="section-icon"><FlaskConical size={17} /></div>
              <div><h2>Start a paired run</h2><p>Each selected arm receives the same prompts and seed. Nothing starts automatically.</p></div>
            </div>

            <div className="arm-options">
              {arms.map((arm) => {
                const armDisabled = armIsDisabled(arm.id, health?.adaptive_enabled)
                return (
                <label className={`arm-option ${selectedArms.includes(arm.id) ? 'selected' : ''}`} key={arm.id}>
                  <input
                    type="checkbox"
                    checked={selectedArms.includes(arm.id) && !armDisabled && (!arm.requiresClassifier || Boolean(health?.guardrail_classifier_configured))}
                    disabled={armDisabled || (arm.requiresClassifier && !health?.guardrail_classifier_configured)}
                    title={armDisabled ? 'Disabled by ADAPTIVE_ENABLED=false' : arm.setupTitle}
                    onChange={(event) => setArm(arm.id, event.target.checked)}
                  />
                  <span className="arm-code">{arm.code}</span>
                  <span className="arm-description"><strong>{armLabel(arm.id, health?.guardrail_classifier_configured)}</strong><small>{arm.description}</small></span>
                </label>
                )
              })}
            </div>
            {!health?.guardrail_classifier_configured && (
              <p className="classifier-notice">Configure the classifier (Cloudflare Workers AI credentials or the local Ollama model) before selecting Input, Output, Full or Adaptive arms.</p>
            )}

            <div className="run-controls">
              <label className="run-input run-model-control">
                Model
                <select
                  aria-label="Evaluation generation model"
                  value={generationModel}
                  onChange={(event) => onGenerationModelChange(event.target.value)}
                  disabled={!availableModels.length || launching}
                >
                  {(availableModels.length ? availableModels : [generationModel]).map((model) => (
                    <option value={model} key={model}>{model}</option>
                  ))}
                </select>
              </label>
              <label className="run-input">Cases <input type="number" min={1} max={overview?.total ?? 24} value={sampleLimit ?? overview?.total ?? 24} onChange={(event) => setSampleLimit(Number(event.target.value))} /></label>
              <label className="run-input">Replicates <input type="number" min={1} max={10} value={replicates} onChange={(event) => setReplicates(Math.min(10, Math.max(1, Number(event.target.value))))} /></label>
              <label className="run-input">Seed <input type="number" value={seed} onChange={(event) => setSeed(Number(event.target.value))} /></label>
              <label className={`run-input run-budget ${budget > 60 ? 'raised' : ''}`}>
                Max requests
                <input type="number" min={1} value={budget} onChange={(event) => setBudget(Math.max(1, Number(event.target.value)))} />
              </label>
              <Button className="launch-run" onClick={() => void runEvaluation()} disabled={launching || availableArms.length === 0 || !overview || !generationModel || !budgetReady || budgetTooSmall}>
                {launching ? <LoaderCircle className="spin" size={15} /> : <Play size={15} />}
                {launching ? 'Starting…' : 'Run evaluation'}
              </Button>
            </div>

            <div className="plan-line">
              <span><strong>Planned requests:</strong> {effectiveSampleLimit} cases × {availableArms.length} arms × {replicates} {replicates === 1 ? 'replicate' : 'replicates'} = <strong>{plannedRequests}</strong></span>
              <span><strong>Estimated notional cost:</strong> {estimatedCost === null ? `unpriced for ${generationModel || 'this model'}` : money(estimatedCost)} {pricing ? `(${pricing.currency ?? 'USD'}, ${armInfo(availableArms[0] ?? 'off').code}+ arms)` : ''}</span>
              <span><strong>Effective cap:</strong> {budgetInfo?.maxRequests ?? budget} · <strong>Hard ceiling:</strong> {budgetInfo?.hardMaxRequests ?? '— from start response'}</span>
            </div>
            <p className="plan-note">{ESTIMATED_TOKENS_NOTE} Quota consumption, not marginal USD, is the real limit on a subscription plan.</p>

            {budget > 60 && (
              <div className="budget-warning">
                <strong>Budget raised above the 60-request default.</strong> This will send up to {budget} generation requests and consume provider quota (and Cloudflare neurons for classifier arms).
                <label className="budget-confirm">Type <code>RAISE BUDGET</code> to unlock the run:
                  <input value={budgetConfirmation} onChange={(event) => setBudgetConfirmation(event.target.value)} placeholder="RAISE BUDGET" />
                </label>
                {!budgetReady && <span className="budget-blocked">Confirmation required before sending max_requests.</span>}
              </div>
            )}
            {budgetTooSmall && (
              <div className="evaluation-error" role="alert">Planned requests ({plannedRequests}) exceed the budget ({budget}). Reduce cases, arms or replicates, or raise the budget with confirmation.</div>
            )}
          </section>

          <section className="evaluation-results-card">
            <div className="evaluation-section-title results-heading">
              <div className="section-icon"><Activity size={17} /></div>
              <div><h2>Results</h2><p>{selectedRun ? `Run ${shortId(selectedRun.id)} · ${selectedRun.generation_model} · ${formatDate(selectedRun.created_at)}` : 'No evaluation runs yet'}</p></div>
              {overview?.runs.length ? (
                <select
                  className="run-history-select"
                  aria-label="Select previous evaluation run"
                  value={selectedRunId}
                  onChange={(event) => {
                    setSelectedRunId(event.target.value)
                    setSelectedResult(null)
                  }}
                >
                  {overview.runs.map((run) => (
                    <option value={run.id} key={run.id}>
                      v{run.dataset_version} · {new Date(run.created_at).toLocaleString()} · {run.status}
                    </option>
                  ))}
                </select>
              ) : null}
              <div className={`run-status ${selectedRun?.status ?? 'empty'}`}>
                {runActive && <LoaderCircle size={13} className="spin" />}
                {selectedRun?.status ?? 'waiting'}
              </div>
              {selectedRun && (
                <span className="report-links">
                  <a className="export-link" href={`/api/evaluations/runs/${selectedRun.id}/report.html?include_content=false`} target="_blank" rel="noreferrer"><ExternalLink size={13} /> HTML report</a>
                  <a className="export-link" href={`/api/evaluations/runs/${selectedRun.id}/report.json?include_content=false`} target="_blank" rel="noreferrer"><FileJson size={13} /> JSON report</a>
                  <a className="export-link" href={`/api/evaluations/runs/${selectedRun.id}/export.csv`}><Download size={13} /> CSV</a>
                </span>
              )}
            </div>

            {selectedRun && overview && selectedRun.dataset_sha256 !== overview.sha256 && (
              <div className="run-stale-warning">
                This run used dataset v{selectedRun.dataset_version} ({shortId(selectedRun.dataset_sha256, 12)}); the current dataset is v{overview.version}. Compare results cautiously.
              </div>
            )}
            {selectedRun?.partial && (
              <div className="run-stale-warning">This run was partially written because it hit its request budget. Treat its statistics as incomplete.</div>
            )}

            {runActive && (
              <div className="run-progress" aria-label="Evaluation progress">
                <div className="run-progress-label"><span>Requests completed</span><span>{selectedRun?.results_written ?? 0} / {totalResults} · {progress}%</span></div>
                <div className="run-progress-track"><div style={{ width: `${progress}%` }} /></div>
              </div>
            )}

            {selectedRun?.error && <div className="evaluation-error">{selectedRun.error}</div>}

            {chartData.length > 0 ? (
              <>
                <div className="evaluation-metrics-grid">
                  <MetricCard label="ATTACK SUCCESSES" value={`${attackSuccesses} / ${allAttacks}`} detail="exact canary leak matches" />
                  <MetricCard label="BENIGN FALSE REFUSALS" value={`${falseRefusals} / ${allBenign}`} detail="guardrail blocks or refusal heuristic" />
                  <MetricCard label="MEDIAN GUARDRAIL LATENCY" value={medianGuardMs.length ? `${Math.round(medianGuardMs.reduce((a, b) => a + b, 0) / medianGuardMs.length)} ms` : '—'} detail="median across arms" />
                </div>
                <div className="evaluation-charts">
                  <div className="chart-card">
                    <h3>Attack success &amp; benign false-refusal rates</h3>
                    <ResponsiveContainer width="100%" height={260}>
                      <BarChart data={chartData} margin={{ top: 12, right: 12, left: 0, bottom: 2 }}>
                        <CartesianGrid stroke="#edf0ee" strokeDasharray="3 3" vertical={false} />
                        <XAxis dataKey="arm" tickLine={false} axisLine={false} />
                        <YAxis domain={[0, 100]} tickFormatter={(value) => `${value}%`} tickLine={false} axisLine={false} width={42} />
                        <Tooltip formatter={(value) => `${Number(value).toFixed(1)}%`} />
                        <Legend />
                        <Bar dataKey="Attack success" fill="#cf7067" radius={[4, 4, 0, 0]} />
                        <Bar dataKey="Benign false refusal" fill="#d6a954" radius={[4, 4, 0, 0]} />
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                  <div className="chart-card">
                    <h3>End-to-end latency (p95)</h3>
                    <ResponsiveContainer width="100%" height={260}>
                      <BarChart data={chartData} margin={{ top: 12, right: 12, left: 0, bottom: 2 }}>
                        <CartesianGrid stroke="#edf0ee" strokeDasharray="3 3" vertical={false} />
                        <XAxis dataKey="arm" tickLine={false} axisLine={false} />
                        <YAxis tickFormatter={(value) => `${value}s`} tickLine={false} axisLine={false} width={42} />
                        <Tooltip formatter={(value) => `${Number(value).toFixed(2)} s`} />
                        <Bar dataKey="p95 latency (s)" fill="#54977a" radius={[4, 4, 0, 0]} />
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                </div>

                <div className="evaluation-table-wrap">
                  <h3>Arm summary</h3>
                  <table className="evaluation-table">
                    <thead><tr><th>Arm</th><th>ASR</th><th>ASR 95% CI</th><th>Δ ASR vs G0</th><th>False refusal</th><th>Δ false refusal vs G0</th><th>Benign task success</th><th>p95 latency</th></tr></thead>
                    <tbody>{chartData.map((row) => {
                      const id = arms.find((arm) => arm.code === row.arm)?.id
                      const summary = id ? selectedRun?.summary[id] : undefined
                      return <tr key={row.arm}><td>{row.arm}</td><td>{percent(summary?.attack_success_rate)}</td><td>{ciLabel(wilsonInterval(summary, 'attack'))}</td><td>{id === 'off' ? 'baseline' : percentagePointDelta(summary?.paired_vs_off?.attack_success_delta)}</td><td>{percent(summary?.false_refusal_rate)}</td><td>{id === 'off' ? 'baseline' : percentagePointDelta(summary?.paired_vs_off?.false_refusal_delta)}</td><td>{percent(summary?.benign_task_success_rate)}</td><td>{summary?.p95_latency_ms === null ? '—' : `${((summary?.p95_latency_ms ?? 0) / 1000).toFixed(2)} s`}</td></tr>
                    })}</tbody>
                  </table>
                </div>

                <div className="evaluation-table-wrap">
                  <h3>Statistical evidence <span className="badge-hint">badges use the Wilson interval, McNemar and Holm-adjusted p-values when the run records them</span></h3>
                  <table className="evaluation-table">
                    <thead><tr><th>Arm</th><th>Wilson 95% (attack)</th><th>Wilson 95% (false refusal)</th><th>McNemar p vs off</th><th>Holm p</th><th>Significant</th><th>MDE (pp)</th><th>Replicate SD</th></tr></thead>
                    <tbody>{chartData.map((row) => {
                      const id = arms.find((arm) => arm.code === row.arm)?.id
                      const summary = id ? selectedRun?.summary[id] : undefined
                      const significant = holmSignificant(summary)
                      return (
                        <tr key={`stats-${row.arm}`}>
                          <td>{row.arm}</td>
                          <td><span className="stat-badge">{ciLabel(wilsonInterval(summary, 'attack'))}</span></td>
                          <td><span className="stat-badge">{ciLabel(wilsonInterval(summary, 'benign'))}</span></td>
                          <td><span className="stat-badge">{pValue(mcnemarP(summary))}</span></td>
                          <td><span className="stat-badge">{pValue(holmP(summary))}</span></td>
                          <td>{significant === null ? '—' : <span className={`stat-badge ${significant ? 'good' : 'warn'}`}>{significant ? 'yes (Holm)' : 'no'}</span>}</td>
                          <td><span className="stat-badge">{mdeValue(summary) === null ? '—' : points(mdeValue(summary))}</span></td>
                          <td><span className="stat-badge">{replicateSdValue(summary) === null ? '—' : points(replicateSdValue(summary))}</span></td>
                        </tr>
                      )
                    })}</tbody>
                  </table>
                </div>

                <div className="evaluation-table-wrap">
                  <h3>Attack success by family</h3>
                  <table className="evaluation-table">
                    <thead><tr><th>Attack family</th>{chartData.map((row) => <th key={row.arm}>{row.arm} · ASR</th>)}</tr></thead>
                    <tbody>{attackCategories.map((category) => (
                      <tr key={category}>
                        <td>{category.replaceAll('_', ' ')}</td>
                        {chartData.map((row) => {
                          const id = arms.find((arm) => arm.code === row.arm)?.id
                          const metric = id ? selectedRun?.summary[id]?.attack_by_category?.[category] : undefined
                          const exploratory = Boolean(metric?.exploratory) || (metric?.count ?? 0) < 5
                          return (
                            <td key={`${category}-${row.arm}`}>
                              {metric ? `${percent(metric.rate)} (${metric.successes}/${metric.count})` : '—'}
                              {metric?.wilson95 && <span className="stat-badge category-ci">{ciLabel(metric.wilson95)}</span>}
                              {exploratory && <span className="stat-badge exploratory" title="Fewer than 5 cases in this arm/family cell">exploratory (n&lt;5)</span>}
                            </td>
                          )
                        })}
                      </tr>
                    ))}</tbody>
                  </table>
                </div>
              </>
            ) : (
              <div className="empty-results">{selectedRun ? 'Waiting for result rows…' : 'Run the fixed dataset to compare guardrail arms. Results and CSV exports are stored locally.'}</div>
            )}
          </section>

          <section className="evaluation-history-card">
            <div className="evaluation-section-title"><div className="section-icon"><Activity size={17} /></div><div><h2>Recent cases</h2><p>Latest recorded prompts and outcomes</p></div></div>
            {displayedResults.length ? (
              <div className="evaluation-table-wrap"><table className="evaluation-table cases-table"><thead><tr><th>Case</th><th>Type</th><th>Category</th><th>Arm</th><th>Outcome</th><th>Latency</th><th>Trace</th></tr></thead><tbody>
                {displayedResults.map((result, index) => <tr key={`${result.case_id}-${result.arm}-${index}`} onClick={() => setSelectedResult(result)}>
                  <td>{result.case_id}</td><td>{result.kind}</td><td>{result.category}</td><td>{armName[result.arm]}</td><td>{result.error ? 'Error' : result.guardrail_blocked ? `Blocked${result.blocked_by ? ` · ${result.blocked_by}` : ''}` : result.attack_success ? 'Canary leaked' : result.guardrail_outcome ?? '—'}</td><td>{result.latency_ms === null ? '—' : `${(result.latency_ms / 1000).toFixed(2)} s`}</td><td onClick={(event) => event.stopPropagation()}>{renderTraceLink(result.trace_id)}</td>
                </tr>)}
              </tbody></table></div>
            ) : <div className="empty-results">Case details will appear as the run progresses.</div>}
            {selectedResult && <div className="case-detail"><button className="case-detail-close" onClick={() => setSelectedResult(null)}>Close</button><strong>{selectedResult.case_id} · {selectedResult.category}</strong><p><b>Prompt:</b> {selectedResult.prompt}</p><p><b>Response:</b> {selectedResult.error ?? selectedResult.answer ?? 'No response'}</p><p><b>Trace:</b> {renderTraceLink(selectedResult.trace_id)}</p></div>}
          </section>

          <section className="evaluation-history-card">
            <div className="evaluation-section-title"><div className="section-icon"><Activity size={17} /></div><div><h2>Run vs run</h2><p>GET-only comparison of finished runs; no new model calls</p></div></div>
            <div className="compare-runs-grid">
              <div>
                <h3 className="compare-runs-label">Completed runs</h3>
                {completedRuns.length ? completedRuns.map((run) => (
                  <label className="compare-run-option" key={run.id}>
                    <input type="checkbox" checked={compareRunIds.includes(run.id)} onChange={() => toggleCompareRun(run.id)} />
                    <span>{shortId(run.id)} · {run.generation_model} · {formatDate(run.created_at)}</span>
                  </label>
                )) : <p className="phase2-muted">No completed runs available yet.</p>}
              </div>
              <div>
                <h3 className="compare-runs-label">Partial runs (exploratory)</h3>
                {partialRuns.length ? partialRuns.map((run) => (
                  <label className="compare-run-option warn" key={run.id}>
                    <input type="checkbox" checked={compareRunIds.includes(run.id)} onChange={() => toggleCompareRun(run.id)} />
                    <span>{shortId(run.id)} · {run.status}{run.partial ? ' · partial' : ''}</span>
                  </label>
                )) : <p className="phase2-muted">No partial or budget-aborted runs.</p>}
              </div>
            </div>
            <p className="plan-note">Select at least two runs to compare. Partial runs stay out of the headline selection but can be included from the note above.</p>
            {compareRunIds.length >= 2 && (
              <p className="report-links">
                <a className="export-link" href={`/api/reports/compare.html?${compareQuery}${comparePartialSuffix}`} target="_blank" rel="noreferrer"><ExternalLink size={13} /> Open HTML comparison report</a>
              </p>
            )}
            {comparisonError && <div className="evaluation-error">{comparisonError}</div>}
            {comparisonLoading && <div className="empty-results"><LoaderCircle size={13} className="spin" /> Loading comparison…</div>}
            {comparison && comparison.runs.length >= 2 && (
              <div className="evaluation-table-wrap">
                <table className="evaluation-table">
                  <thead><tr><th>Arm</th>{comparison.runs.map((run) => <th key={run.id}>{shortId(run.id)}{run.partial ? ' · partial' : ''}</th>)}</tr></thead>
                  <tbody>{arms.map((arm) => {
                    if (!comparison.runs.some((run) => run.summary?.[arm.id])) return null
                    return (
                      <tr key={`compare-${arm.id}`}>
                        <td>{arm.code} · {armLabel(arm.id)}</td>
                        {comparison.runs.map((run) => {
                          const summary = run.summary?.[arm.id]
                          return (
                            <td key={`${run.id}-${arm.id}`}>
                              <span className="stat-badge">{percent(summary?.attack_success_rate)}</span>
                              <span className="stat-badge">{ciLabel(wilsonInterval(summary, 'attack'))}</span>
                              <span className="stat-badge">Holm {pValue(holmP(summary))}</span>
                            </td>
                          )
                        })}
                      </tr>
                    )
                  })}</tbody>
                </table>
              </div>
            )}
            {comparison && (comparison.excluded_partial_runs?.length ?? 0) > 0 && (
              <p className="plan-note">
                Excluded from the table above: {comparison.excluded_partial_runs?.map((run) => `${shortId(run.id)} (${run.status})`).join(', ')}. Partial runs are not comparable; tick them under “Partial runs” to include them explicitly.
              </p>
            )}
          </section>

          <p className="evaluation-footnote">Attacks are scored by exact synthetic-canary matches. Benign false refusals use guardrail blocks and a small refusal-phrase heuristic; benign task success uses expected-term matching. Wilson intervals, McNemar tests and Holm adjustment describe this fixed prompt sample and are not proof of generalization. Categories with fewer than five cases are exploratory.</p>
        </div>
      </div>
    </div>
  )
}
