import { useCallback, useEffect, useState } from 'react'
import { ArrowLeft, Coins, ExternalLink, LoaderCircle, RefreshCw } from 'lucide-react'
import { Button } from '../components/ui/button'
import {
  getJson,
  money,
  normalisePricingModels,
  type PricingDocument,
  type PricingModel,
} from '../lib/phase2'
import './Phase2Pages.css'

type ModelsPageProps = { onBackToChat: () => void }

function isPriced(model: PricingModel): boolean {
  if (model.priced !== undefined) return model.priced
  if (model.free) return true
  return model.input_per_mtok !== null && model.input_per_mtok !== undefined
    && model.output_per_mtok !== null && model.output_per_mtok !== undefined
}

function rate(value: number | null | undefined, currency: string): string {
  return value === null || value === undefined ? 'unpriced' : `${money(value, currency)} / MTok`
}

function allowance(model: PricingModel): string {
  const unit = model.allowance_unit
  const limit = model.allowance_limit
  const window = model.allowance_window
  if (!unit && limit === null && limit === undefined && !window) return 'plan-level'
  return [limit !== null && limit !== undefined ? String(limit) : null, unit, window]
    .filter(Boolean)
    .join(' · ')
}

function guardrailStatus(entry: Record<string, unknown>): { label: string; tone: string } {
  if (entry.billing === 'local' || entry.usd_per_request === 0) {
    return { label: 'local · $0 marginal', tone: 'good' }
  }
  const models = entry.models
  if (models && typeof models === 'object') {
    const hasRate = Object.values(models as Record<string, Record<string, unknown>>).some(
      (model) => model && model.neurons_per_million_input_tokens != null,
    )
    if (hasRate) return { label: 'priced', tone: 'good' }
  }
  return { label: 'unpriced per token', tone: 'warn' }
}

function external(value: unknown) {
  if (typeof value !== 'string' || !value) return null
  return (
    <a className="phase2-inline-link" href={value} target="_blank" rel="noreferrer">
      {value.replace(/^https?:\/\//, '').slice(0, 42)} <ExternalLink size={10} />
    </a>
  )
}

export function ModelsPage({ onBackToChat }: ModelsPageProps) {
  const [pricing, setPricing] = useState<PricingDocument | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setPricing(await getJson<PricingDocument>('/api/pricing'))
      setError(null)
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : 'Could not load the pricing catalog.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const models = normalisePricingModels(pricing)
  const currency = pricing?.currency ?? 'USD'
  const checksum = pricing?.sha256 ?? pricing?.pricing_sha256
  const guardrails = Object.entries(pricing?.guardrails ?? {})

  return (
    <div className="phase2-page">
      <header className="phase2-topbar">
        <div className="breadcrumb"><span>Workspace</span><span className="breadcrumb-divider">/</span><strong>Models / Cost</strong></div>
        <Button variant="outline" size="sm" onClick={onBackToChat}>
          <ArrowLeft size={14} /> Chat playground
        </Button>
      </header>
      <div className="phase2-scroll">
        <div className="phase2-content">
          <section className="phase2-heading">
            <div className="eyebrow">PRICING SNAPSHOT</div>
            <h1>Models &amp; cost</h1>
            <p>Committed, checksummed price catalog used for accounting. All generation rates are USD per million tokens (MTok).</p>
          </section>

          {error && <div className="phase2-error" role="alert">{error}</div>}

          <div className="models-callout">
            <strong>Notional vs marginal cost.</strong> Notional cost is the list-price equivalent, useful for comparing models. Marginal cost is what one extra request bills: on a subscription plan it is <strong>$0</strong> inside the allowance, and quota consumption is the real constraint. Unpriced models show <em>unpriced</em> rather than an imputed number.
          </div>

          <div className="phase2-card">
            <div className="phase2-section-title">
              <div className="phase2-section-icon"><Coins size={17} /></div>
              <div>
                <h2>Catalog provenance</h2>
                <p>{pricing ? `${pricing.provider ?? 'unknown provider'} · schema v${pricing.schema_version ?? '—'} · ${pricing.billing_mode ?? 'unknown billing'}` : 'Loading catalog…'}</p>
              </div>
              <button className="phase2-inline-button" onClick={() => void load()} aria-label="Reload pricing catalog" title="Reload catalog">
                <RefreshCw size={13} />
              </button>
            </div>
            {loading ? (
              <div className="trace-empty"><LoaderCircle size={14} className="spin" /> Loading pricing…</div>
            ) : (
              <div className="models-provenance">
                <span className="phase2-badge">retrieved {pricing?.retrieved_at ?? '—'}</span>
                <span className="phase2-badge" title={checksum ?? undefined}>sha256 {checksum ? checksum.slice(0, 12) : '—'}</span>
                {external(pricing?.source_url)}
                {external(pricing?.provider_doc_url)}
              </div>
            )}
          </div>

          <div className="phase2-card">
            <div className="phase2-section-title">
              <div className="phase2-section-icon"><Coins size={17} /></div>
              <div><h2>Generation models</h2><p>{models.length} entries in snapshot</p></div>
            </div>
            {models.length ? (
              <div className="phase2-table-wrap">
                <table className="phase2-table">
                  <thead><tr>
                    <th>Model ID</th><th>Compatibility / notes</th><th>Input</th><th>Output</th>
                    <th>Free</th><th>Allowance (unit · limit · window)</th><th>Status</th>
                  </tr></thead>
                  <tbody>
                    {models.map((model) => (
                      <tr key={model.id}>
                        <td className="phase2-code">{model.id}</td>
                        <td>{model.notes ?? '—'}</td>
                        <td className="phase2-code">{rate(model.input_per_mtok, currency)}</td>
                        <td className="phase2-code">{rate(model.output_per_mtok, currency)}</td>
                        <td>{model.free ? 'yes' : 'no'}</td>
                        <td>{allowance(model)}</td>
                        <td>
                          <span className={`phase2-badge ${isPriced(model) ? 'good' : 'warn'}`}>
                            {isPriced(model) ? 'priced' : 'unpriced'}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="trace-empty">No model pricing available from the catalog endpoint.</div>
            )}
          </div>

          <div className="phase2-card">
            <div className="phase2-section-title">
              <div className="phase2-section-icon"><Coins size={17} /></div>
              <div><h2>Guardrail pricing</h2><p>Local scanners and the classifier have separate accounting</p></div>
            </div>
            <div className="models-guardrails">
              {guardrails.length ? guardrails.map(([name, entry]) => {
                const status = guardrailStatus(entry)
                const pricePerRequest = entry.usd_per_request
                const neurons = entry.usd_per_million_neurons
                const freeNeurons = entry.free_neurons_per_day
                return (
                  <div className="models-guardrail" key={name}>
                    <strong>{name}</strong>
                    <p>Billing: {String(entry.billing ?? '—')}</p>
                    <p>
                      Price: {typeof pricePerRequest === 'number'
                        ? `${money(pricePerRequest, currency)} / request`
                        : typeof neurons === 'number'
                          ? `${money(neurons, currency)} / million neurons`
                          : 'not published'}
                    </p>
                    {typeof freeNeurons === 'number' && <p>Free allowance: {freeNeurons.toLocaleString()} neurons / day</p>}
                    {typeof entry.notes === 'string' && entry.notes.length > 0 && <p>{entry.notes}</p>}
                    {typeof entry.source_url === 'string' && entry.source_url.length > 0 && <p>Source: {external(entry.source_url)}</p>}
                    <span className={`phase2-badge ${status.tone}`}>{status.label}</span>
                  </div>
                )
              }) : (
                <div className="trace-empty">No guardrail pricing in this snapshot.</div>
              )}
            </div>
          </div>

          <p className="phase2-note">
            Allowance columns are populated only when the catalog records a per-model allowance; OpenCode Go tracks quota at the plan level. Prices are never fetched at request time.
          </p>
        </div>
      </div>
    </div>
  )
}
