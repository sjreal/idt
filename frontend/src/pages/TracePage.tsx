import { useCallback, useEffect, useMemo, useState } from 'react'
import { Activity, ArrowLeft, ExternalLink, LoaderCircle, RefreshCw, Route } from 'lucide-react'
import { Button } from '../components/ui/button'
import {
  costLabel,
  getJson,
  shortId,
  verdictOutcome,
  type GuardrailStage,
  type ObservabilityConfig,
  type TraceRecord,
} from '../lib/phase2'
import './Phase2Pages.css'

type TraceListResponse = { traces: TraceRecord[]; dropped_total: number }

type TracePageProps = {
  initialTraceId?: string | null
  onBackToChat: () => void
}

function formatDate(value: string | null | undefined) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function formatMs(value: number | null | undefined) {
  return typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(1)} ms` : '—'
}

function stageOutcomeClass(outcome: string | undefined) {
  if (outcome === 'blocked') return 'bad'
  if (outcome === 'redacted') return 'warn'
  if (outcome === 'review') return 'warn'
  if (outcome === 'passed' || outcome === 'skipped') return 'good'
  return ''
}

function SpanTimeline({ trace }: { trace: TraceRecord }) {
  const spans = trace.spans ?? []
  if (!spans.length) {
    return <div className="trace-empty">No spans recorded for this trace.</div>
  }
  const end = Math.max(
    trace.total_latency_ms ?? 0,
    ...spans.map((span) => span.start_offset_ms + span.duration_ms),
    1,
  )
  const barHeight = Math.max(8, Math.min(18, Math.floor(64 / spans.length)))
  return (
    <div>
      <div className="trace-timeline" style={{ height: Math.max(64, 28 + spans.length * (barHeight + 4)) }}>
        <span className="trace-timeline-label">span timeline · {formatMs(end)} total</span>
        {spans.map((span, index) => (
          <div
            key={`${span.name}-${index}`}
            className="trace-timeline-bar"
            title={`${span.name} · +${formatMs(span.start_offset_ms)} · ${formatMs(span.duration_ms)}`}
            style={{
              top: 17 + index * (barHeight + 4),
              left: `${Math.min(98, Math.max(0, (span.start_offset_ms / end) * 100))}%`,
              width: `${Math.max(0.6, Math.min(100 - (span.start_offset_ms / end) * 100, (span.duration_ms / end) * 100))}%`,
              height: barHeight,
            }}
          />
        ))}
      </div>
      <div className="trace-timeline-legend">
        {spans.map((span, index) => (
          <span key={`${span.name}-legend-${index}`} className="phase2-code">
            {span.name} ({formatMs(span.duration_ms)})
          </span>
        ))}
      </div>
    </div>
  )
}

function VerdictDetail({ verdict }: { verdict: TraceRecord['verdict'] }) {
  if (!verdict) return <p className="phase2-muted">No guardrail verdict recorded.</p>
  const stages: GuardrailStage[] = verdict.stages ?? []
  return (
    <div>
      <div className="phase2-actions" style={{ marginBottom: 9 }}>
        <span className={`phase2-badge ${stageOutcomeClass(verdict.outcome)}`}>{verdict.outcome}</span>
        <span className="phase2-badge">{verdict.level}</span>
        {verdict.blocked && <span className="phase2-badge bad">blocked · {verdict.blocked_by ?? 'guardrail'}</span>}
        {verdict.category && <span className="phase2-badge warn">{verdict.category}</span>}
        <span className="phase2-badge">{formatMs(verdict.latency_ms)}</span>
      </div>
      {stages.length > 0 && (
        <div className="trace-stage-list">
          {stages.map((stage, index) => (
            <div className="trace-stage" key={`${stage.stage}-${index}`}>
              <strong className="phase2-code">{stage.stage}</strong>
              <div className="phase2-actions" style={{ marginTop: 4 }}>
                <span className={`phase2-badge ${stageOutcomeClass(stage.outcome)}`}>{stage.outcome}</span>
                <span className="phase2-badge">{stage.backend}</span>
                <span className="phase2-badge">{formatMs(stage.latency_ms)}</span>
                {stage.blocked_by && <span className="phase2-badge bad">{stage.blocked_by}</span>}
                {stage.category && <span className="phase2-badge warn">{stage.category}</span>}
              </div>
              {stage.findings?.length ? (
                <ul className="trace-findings">
                  {stage.findings.map((finding, findingIndex) => (
                    <li key={`${finding.scanner}-${findingIndex}`}>
                      {finding.scanner} · {finding.outcome}
                      {finding.category ? ` · ${finding.category}` : ''}
                      {finding.risk_score !== null && finding.risk_score !== undefined
                        ? ` · score ${finding.risk_score}`
                        : ''}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          ))}
        </div>
      )}
      {verdict.classifier_calls ? (
        <p className="phase2-note">Classifier calls: {verdict.classifier_calls}</p>
      ) : null}
      {verdict.prefilter && Object.keys(verdict.prefilter).length > 0 && (
        <p className="phase2-note">Prefilter: {JSON.stringify(verdict.prefilter)}</p>
      )}
    </div>
  )
}

export function TracePage({ initialTraceId, onBackToChat }: TracePageProps) {
  const [traces, setTraces] = useState<TraceRecord[]>([])
  const [droppedTotal, setDroppedTotal] = useState(0)
  const [obs, setObs] = useState<ObservabilityConfig | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loadingList, setLoadingList] = useState(true)
  const [kindFilter, setKindFilter] = useState('all')
  const [armFilter, setArmFilter] = useState('all')
  const [selectedId, setSelectedId] = useState<string | null>(initialTraceId ?? null)
  const [selectedTrace, setSelectedTrace] = useState<TraceRecord | null>(null)
  const [traceLoading, setTraceLoading] = useState(false)
  const [traceError, setTraceError] = useState<string | null>(null)

  const loadList = useCallback(async () => {
    setLoadingList(true)
    try {
      const response = await getJson<TraceListResponse>('/api/traces?limit=50')
      setTraces(response.traces ?? [])
      setDroppedTotal(response.dropped_total ?? 0)
      setError(null)
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : 'Could not load traces.')
    } finally {
      setLoadingList(false)
    }
  }, [])

  useEffect(() => {
    void loadList()
    void getJson<ObservabilityConfig>('/api/observability/config')
      .then(setObs)
      .catch(() => setObs(null))
  }, [loadList])

  useEffect(() => {
    if (!selectedId) {
      setSelectedTrace(null)
      setTraceError(null)
      return
    }
    let active = true
    setTraceLoading(true)
    setTraceError(null)
    void getJson<TraceRecord>(`/api/traces/${encodeURIComponent(selectedId)}`)
      .then((record) => { if (active) setSelectedTrace(record) })
      .catch((loadError: unknown) => {
        if (!active) return
        setSelectedTrace(null)
        const detail = loadError instanceof Error ? loadError.message : 'Unknown error'
        setTraceError(
          /not found|retention/i.test(detail)
            ? 'trace unavailable (retention expired)'
            : detail,
        )
      })
      .finally(() => { if (active) setTraceLoading(false) })
    return () => { active = false }
  }, [selectedId])

  const kindOptions = useMemo(
    () => [...new Set(traces.map((trace) => trace.kind ?? 'unknown'))].sort(),
    [traces],
  )
  const armOptions = useMemo(
    () => [...new Set(traces.map((trace) => trace.arm ?? 'unknown'))].sort(),
    [traces],
  )
  const filtered = useMemo(
    () => traces.filter(
      (trace) =>
        (kindFilter === 'all' || (trace.kind ?? 'unknown') === kindFilter) &&
        (armFilter === 'all' || (trace.arm ?? 'unknown') === armFilter),
    ),
    [traces, kindFilter, armFilter],
  )

  const usage = selectedTrace?.usage ?? null
  const cost = selectedTrace?.cost

  return (
    <div className="phase2-page">
      <header className="phase2-topbar">
        <div className="breadcrumb"><span>Workspace</span><span className="breadcrumb-divider">/</span><strong>Traces</strong></div>
        <Button variant="outline" size="sm" onClick={onBackToChat}>
          <ArrowLeft size={14} /> Chat playground
        </Button>
      </header>
      <div className="phase2-scroll">
        <div className="phase2-content">
          <section className="phase2-heading">
            <div className="eyebrow">REQUEST OBSERVABILITY</div>
            <h1>Trace viewer</h1>
            <p>Inspect the local trace buffer: span timing, guardrail stages, cost and request metadata. Tracing is optional; nothing here requires an external collector.</p>
          </section>

          {error && <div className="phase2-error" role="alert">{error}</div>}

          <div className="trace-links" style={{ marginBottom: 13 }}>
            <span className={`phase2-badge ${obs?.tracing_enabled ? 'good' : 'warn'}`}>
              {obs ? (obs.tracing_enabled ? 'tracing enabled' : 'local buffer only') : 'trace config unavailable'}
            </span>
            {obs?.content_capture_enabled && <span className="phase2-badge warn">content capture on</span>}
            {obs?.phoenix_url && <a className="phase2-inline-link" href={obs.phoenix_url} target="_blank" rel="noreferrer">Phoenix <ExternalLink size={11} /></a>}
            {obs?.grafana_url && <a className="phase2-inline-link" href={obs.grafana_url} target="_blank" rel="noreferrer">Grafana <ExternalLink size={11} /></a>}
            {obs?.prometheus_url && <a className="phase2-inline-link" href={obs.prometheus_url} target="_blank" rel="noreferrer">Prometheus <ExternalLink size={11} /></a>}
          </div>
          <p className="phase2-note">The trace plane is optional: the local JSONL buffer powers this viewer even when Phoenix, Grafana and Prometheus are not deployed.</p>

          <div className="trace-layout" style={{ marginTop: 13 }}>
            <div className="phase2-card">
              <div className="phase2-section-title">
                <div className="phase2-section-icon"><Route size={17} /></div>
                <div><h2>Recent traces</h2><p>{traces.length} buffered · {droppedTotal} dropped</p></div>
                <button className="phase2-inline-button" onClick={() => void loadList()} title="Reload trace list" aria-label="Reload trace list">
                  <RefreshCw size={13} />
                </button>
              </div>
              <div className="phase2-actions" style={{ marginBottom: 10 }}>
                <label className="phase2-field">Kind
                  <select value={kindFilter} onChange={(event) => setKindFilter(event.target.value)}>
                    <option value="all">All kinds</option>
                    {kindOptions.map((kind) => <option value={kind} key={kind}>{kind}</option>)}
                  </select>
                </label>
                <label className="phase2-field">Arm
                  <select value={armFilter} onChange={(event) => setArmFilter(event.target.value)}>
                    <option value="all">All arms</option>
                    {armOptions.map((arm) => <option value={arm} key={arm}>{arm}</option>)}
                  </select>
                </label>
              </div>
              <div className="trace-list">
                {loadingList ? (
                  <div className="trace-empty"><LoaderCircle size={14} className="spin" /> Loading traces…</div>
                ) : filtered.length ? filtered.map((trace) => (
                  <button
                    className={`trace-row ${selectedId === trace.trace_id ? 'is-selected' : ''}`}
                    key={trace.trace_id}
                    onClick={() => setSelectedId(trace.trace_id)}
                  >
                    <span className="trace-row-top">
                      <span className="trace-row-id">{shortId(trace.trace_id, 12)}</span>
                      <span className={`phase2-badge ${trace.status === 'ok' || trace.status === 'completed' ? 'good' : ''}`}>{trace.status ?? '—'}</span>
                    </span>
                    <span className="trace-row-sub">
                      {formatDate(trace.created_at)} · {trace.kind ?? 'unknown'} · {trace.arm ?? '—'} · {formatMs(trace.total_latency_ms)}
                    </span>
                  </button>
                )) : (
                  <div className="trace-empty">No traces match these filters yet. Send a chat message to create one.</div>
                )}
              </div>
            </div>

            <div className="phase2-card">
              <div className="phase2-section-title">
                <div className="phase2-section-icon"><Activity size={17} /></div>
                <div>
                  <h2>{selectedId ? `Trace ${shortId(selectedId, 12)}` : 'Trace detail'}</h2>
                  <p>{selectedTrace ? `${selectedTrace.route ?? '—'} · ${selectedTrace.model ?? '—'}` : 'Select a trace to inspect its spans'}</p>
                </div>
              </div>
              {!selectedId && <div className="trace-empty">Choose a trace from the list, or open one from a chat message or evaluation result.</div>}
              {traceLoading && <div className="trace-empty"><LoaderCircle size={14} className="spin" /> Loading trace…</div>}
              {traceError && <div className="phase2-error" role="alert">{traceError}</div>}
              {selectedTrace && !traceLoading && (
                <>
                  <div className="trace-detail-grid">
                    <div className="trace-detail-item"><span>CREATED</span><strong title={selectedTrace.created_at}>{formatDate(selectedTrace.created_at)}</strong></div>
                    <div className="trace-detail-item"><span>STATUS</span><strong>{selectedTrace.status ?? '—'}</strong></div>
                    <div className="trace-detail-item"><span>KIND / ROUTE</span><strong>{selectedTrace.kind ?? '—'} · {selectedTrace.route ?? '—'}</strong></div>
                    <div className="trace-detail-item"><span>ARM</span><strong>{selectedTrace.arm ?? '—'}</strong></div>
                    <div className="trace-detail-item"><span>MODEL / BACKEND</span><strong>{selectedTrace.model ?? '—'} · {selectedTrace.backend ?? '—'}</strong></div>
                    <div className="trace-detail-item"><span>REQUEST ID</span><strong title={selectedTrace.request_id ?? undefined}>{shortId(selectedTrace.request_id, 12)}</strong></div>
                    <div className="trace-detail-item"><span>SESSION ID</span><strong title={selectedTrace.session_id ?? undefined}>{shortId(selectedTrace.session_id, 12)}</strong></div>
                    <div className="trace-detail-item"><span>TOTAL LATENCY</span><strong>{formatMs(selectedTrace.total_latency_ms)}</strong></div>
                    <div className="trace-detail-item"><span>TOKENS</span><strong>{usage ? `${usage.prompt_tokens} in / ${usage.completion_tokens} out / ${usage.total_tokens} total` : 'not captured'}</strong></div>
                    <div className="trace-detail-item"><span>COST</span><strong>{costLabel(cost) ?? '—'}</strong></div>
                  </div>
                  {selectedTrace.error && <div className="phase2-error">Error: {selectedTrace.error}</div>}

                  <h3 className="phase2-muted" style={{ fontSize: 9, margin: '0 0 4px' }}>Span timeline</h3>
                  <SpanTimeline trace={selectedTrace} />

                  <h3 className="phase2-muted" style={{ fontSize: 9, margin: '0 0 8px' }}>Guardrail verdict</h3>
                  <VerdictDetail verdict={selectedTrace.verdict} />
                  {verdictOutcome(selectedTrace.verdict) === '—' && (
                    <p className="phase2-muted">No guardrail checks recorded for this request.</p>
                  )}
                </>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
