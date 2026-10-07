import { useCallback, useEffect, useMemo, useState } from 'react'
import { Activity, ArrowLeft, Download, FlaskConical, LoaderCircle, Play } from 'lucide-react'
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
import './EvaluationPage.css'

type Arm = 'off' | 'scanners' | 'full'
type GuardrailBackend = 'cloudflare' | 'ollama'

type Health = {
  llm_connected: boolean
  guardrail_classifier_backend: GuardrailBackend
  guardrail_classifier_configured: boolean
}

type ArmSummary = {
  total: number
  completed: number
  errors: number
  attack_count: number
  attack_successes: number
  attack_success_rate: number | null
  attack_success_ci95: { low: number; high: number } | null
  attack_by_category: Record<string, { count: number; successes: number; rate: number; ci95: { low: number; high: number } | null }>
  benign_count: number
  false_refusals: number
  false_refusal_rate: number | null
  false_refusal_ci95: { low: number; high: number } | null
  benign_task_success_rate: number | null
  guardrail_blocked: number
  p50_latency_ms: number | null
  p95_latency_ms: number | null
  p50_guardrail_latency_ms: number | null
  paired_vs_off?: {
    attack_success_delta: number | null
    attack_success_delta_ci95: { low: number; high: number } | null
    false_refusal_delta: number | null
    false_refusal_delta_ci95: { low: number; high: number } | null
  }
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
}

type Run = {
  id: string
  status: 'queued' | 'running' | 'completed' | 'completed_with_errors' | 'failed'
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
  runs: Run[]
}

type EvaluationPageProps = {
  health: Health | null
  generationModel: string
  availableModels: string[]
  onGenerationModelChange: (model: string) => void
  onBackToChat: () => void
}

const arms: { id: Arm; title: string; code: string; description: string }[] = [
  { id: 'off', title: 'Baseline', code: 'G0', description: 'No input/output guardrails' },
  { id: 'scanners', title: 'Scanners', code: 'G1', description: 'LLM Guard input scanners' },
  { id: 'full', title: 'Full guardrails', code: 'G2', description: 'Scanners + Llama Guard' },
]

const armName: Record<Arm, string> = { off: 'G0 · Off', scanners: 'G1 · Scanners', full: 'G2 · Full' }

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url)
  const body = await response.json()
  if (!response.ok) {
    throw new Error(typeof body.detail === 'string' ? body.detail : `Request failed (${response.status})`)
  }
  return body as T
}

function percent(value: number | null | undefined) {
  return value === null || value === undefined ? '—' : `${(value * 100).toFixed(1)}%`
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
}: EvaluationPageProps) {
  const [overview, setOverview] = useState<Overview | null>(null)
  const [selectedRunId, setSelectedRunId] = useState('')
  const [selectedRun, setSelectedRun] = useState<Run | null>(null)
  const [selectedResult, setSelectedResult] = useState<Result | null>(null)
  const [selectedArms, setSelectedArms] = useState<Arm[]>(['off', 'scanners', 'full'])
  const [sampleLimit, setSampleLimit] = useState<number | null>(null)
  const [seed, setSeed] = useState(42)
  const [launching, setLaunching] = useState(false)
  const [error, setError] = useState<string | null>(null)

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

  const runEvaluation = async () => {
    if (!overview || availableArms.length === 0 || !generationModel || launching) return
    const effectiveSampleLimit = sampleLimit ?? overview.total
    const requestCount = effectiveSampleLimit * availableArms.length
    const confirmText = `This sends up to ${requestCount} generation requests across ${availableArms.length} arm(s). Full mode also calls Cloudflare Workers AI and uses its daily free quota. Start?`
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
          generation_model: generationModel,
        }),
      })
      const body = await response.json()
      if (!response.ok) {
        throw new Error(typeof body.detail === 'string' ? body.detail : `Could not start run (${response.status})`)
      }
      setSelectedRunId(body.id as string)
      setSelectedResult(null)
      await refresh()
    } catch (runError) {
      setError(runError instanceof Error ? runError.message : 'Could not start the evaluation.')
    } finally {
      setLaunching(false)
    }
  }

  const runActive = selectedRun?.status === 'queued' || selectedRun?.status === 'running'
  const availableArms = selectedArms.filter(
    (arm) => arm !== 'full' || health?.guardrail_classifier_configured,
  )
  const totalResults = selectedRun ? selectedRun.sample_count * selectedRun.arms.length : 0
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
              <p>Compare attack resistance, benign false refusals, and response latency across guardrail modes.</p>
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
              <div><h2>Start a paired run</h2><p>Each selected arm receives the same prompts and seed.</p></div>
            </div>

            <div className="arm-options">
              {arms.map((arm) => (
                <label className={`arm-option ${selectedArms.includes(arm.id) ? 'selected' : ''}`} key={arm.id}>
                  <input
                    type="checkbox"
                    checked={selectedArms.includes(arm.id) && (arm.id !== 'full' || Boolean(health?.guardrail_classifier_configured))}
                    disabled={arm.id === 'full' && !health?.guardrail_classifier_configured}
                    onChange={(event) => setArm(arm.id, event.target.checked)}
                  />
                  <span className="arm-code">{arm.code}</span>
                  <span className="arm-description"><strong>{arm.title}</strong><small>{arm.description}</small></span>
                </label>
              ))}
            </div>
            {!health?.guardrail_classifier_configured && (
              <p className="classifier-notice">Configure Cloudflare Workers AI credentials in `.env` before selecting Full mode.</p>
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
              <label className="run-input">Seed <input type="number" value={seed} onChange={(event) => setSeed(Number(event.target.value))} /></label>
              <span className="run-estimate">Up to {(sampleLimit ?? overview?.total ?? 24) * availableArms.length} requests · Cloudflare quota applies in Full mode</span>
              <Button className="launch-run" onClick={() => void runEvaluation()} disabled={launching || availableArms.length === 0 || !overview || !generationModel}>
                {launching ? <LoaderCircle className="spin" size={15} /> : <Play size={15} />}
                {launching ? 'Starting…' : 'Run evaluation'}
              </Button>
            </div>
          </section>

          <section className="evaluation-results-card">
            <div className="evaluation-section-title results-heading">
              <div className="section-icon"><Activity size={17} /></div>
              <div><h2>Results</h2><p>{selectedRun ? `Run ${selectedRun.id.slice(0, 8)} · ${selectedRun.generation_model} · ${formatDate(selectedRun.created_at)}` : 'No evaluation runs yet'}</p></div>
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
              {selectedRun && <a className="export-link" href={`/api/evaluations/runs/${selectedRun.id}/export.csv`}><Download size={14} /> CSV</a>}
            </div>

            {selectedRun && overview && selectedRun.dataset_sha256 !== overview.sha256 && (
              <div className="run-stale-warning">
                This run used dataset v{selectedRun.dataset_version} ({selectedRun.dataset_sha256.slice(0, 12)}); the current dataset is v{overview.version}. Compare results cautiously.
              </div>
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
                      return <tr key={row.arm}><td>{row.arm}</td><td>{percent(summary?.attack_success_rate)}</td><td>{summary?.attack_success_ci95 ? `${percent(summary.attack_success_ci95.low)} – ${percent(summary.attack_success_ci95.high)}` : '—'}</td><td>{id === 'off' ? 'baseline' : percentagePointDelta(summary?.paired_vs_off?.attack_success_delta)}</td><td>{percent(summary?.false_refusal_rate)}</td><td>{id === 'off' ? 'baseline' : percentagePointDelta(summary?.paired_vs_off?.false_refusal_delta)}</td><td>{percent(summary?.benign_task_success_rate)}</td><td>{summary?.p95_latency_ms === null ? '—' : `${((summary?.p95_latency_ms ?? 0) / 1000).toFixed(2)} s`}</td></tr>
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
                          return <td key={`${category}-${row.arm}`}>{metric ? `${percent(metric.rate)} (${metric.successes}/${metric.count})` : '—'}</td>
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
              <div className="evaluation-table-wrap"><table className="evaluation-table cases-table"><thead><tr><th>Case</th><th>Type</th><th>Category</th><th>Arm</th><th>Outcome</th><th>Latency</th></tr></thead><tbody>
                {displayedResults.map((result, index) => <tr key={`${result.case_id}-${result.arm}-${index}`} onClick={() => setSelectedResult(result)}>
                  <td>{result.case_id}</td><td>{result.kind}</td><td>{result.category}</td><td>{armName[result.arm]}</td><td>{result.error ? 'Error' : result.guardrail_blocked ? `Blocked${result.blocked_by ? ` · ${result.blocked_by}` : ''}` : result.attack_success ? 'Canary leaked' : result.guardrail_outcome ?? '—'}</td><td>{result.latency_ms === null ? '—' : `${(result.latency_ms / 1000).toFixed(2)} s`}</td>
                </tr>)}
              </tbody></table></div>
            ) : <div className="empty-results">Case details will appear as the run progresses.</div>}
            {selectedResult && <div className="case-detail"><button className="case-detail-close" onClick={() => setSelectedResult(null)}>Close</button><strong>{selectedResult.case_id} · {selectedResult.category}</strong><p><b>Prompt:</b> {selectedResult.prompt}</p><p><b>Response:</b> {selectedResult.error ?? selectedResult.answer ?? 'No response'}</p></div>}
          </section>

          <p className="evaluation-footnote">Attacks are scored by exact synthetic-canary matches. Benign false refusals use guardrail blocks and a small refusal-phrase heuristic; benign task success uses expected-term matching. Bootstrap intervals describe this fixed prompt sample and are not proof of generalization.</p>
        </div>
      </div>
    </div>
  )
}
