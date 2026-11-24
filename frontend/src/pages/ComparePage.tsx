import { useEffect, useState } from 'react'
import { ArrowLeft, GitCompareArrows, LoaderCircle, Plus, Send } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import rehypeSanitize from 'rehype-sanitize'
import remarkGfm from 'remark-gfm'
import { Button } from '../components/ui/button'
import {
  GUARDRAIL_ARMS,
  armLabel,
  costLabel,
  getJson,
  shortId,
  verdictOutcome,
  type CompareResponse,
  type CompareVariantResult,
  type GuardrailArm,
  type GuardrailVerdict,
  type ModelListResponse,
} from '../lib/phase2'
import './Phase2Pages.css'

type Health = {
  model?: string
  guardrail_classifier_configured: boolean
  guardrail_classifier_backend?: 'cloudflare' | 'ollama'
}

type Variant = { model: string; arm: GuardrailArm }

type ComparePageProps = {
  onBackToChat: () => void
  onOpenTrace: (traceId: string) => void
}

function guardrailObject(value: CompareVariantResult['guardrail']): GuardrailVerdict | null {
  return value && typeof value === 'object' ? value : null
}

export function ComparePage({ onBackToChat, onOpenTrace }: ComparePageProps) {
  const [prompt, setPrompt] = useState('')
  const [models, setModels] = useState<string[]>([])
  const [model, setModel] = useState('')
  const [arm, setArm] = useState<GuardrailArm>('off')
  const [configured, setConfigured] = useState(false)
  const [classifierBackend, setClassifierBackend] = useState<'cloudflare' | 'ollama' | null>(null)
  const [variants, setVariants] = useState<Variant[]>([
    { model: '', arm: 'off' },
    { model: '', arm: 'scanners' },
  ])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [results, setResults] = useState<CompareVariantResult[]>([])

  useEffect(() => {
    let active = true
    void getJson<Health>('/health')
      .then((health) => {
        if (!active) return
        setConfigured(health.guardrail_classifier_configured)
        setClassifierBackend(health.guardrail_classifier_backend ?? null)
        if (health.model) {
          setModel(health.model)
          setVariants((current) => current.map((variant) => (
            variant.model ? variant : { ...variant, model: health.model as string }
          )))
        }
      })
      .catch(() => undefined)
    void getJson<ModelListResponse>('/v1/models')
      .then((response) => {
        if (!active) return
        const ids = response.data.map((entry) => entry.id)
        setModels(ids)
        if (ids.length) {
          setModel((current) => current || ids[0])
          setVariants((current) => current.map((variant) => (
            variant.model ? variant : { ...variant, model: ids[0] }
          )))
        }
      })
      .catch(() => undefined)
    return () => { active = false }
  }, [])

  const setVariant = (index: number, patch: Partial<Variant>) => {
    setVariants((current) => current.map((variant, position) => (
      position === index ? { ...variant, ...patch } : variant
    )))
  }

  const addVariant = () => {
    setVariants((current) => current.length >= 4 ? current : [...current, { model: model || models[0] || '', arm }])
  }

  const removeVariant = (index: number) => {
    setVariants((current) => current.length <= 2 ? current : current.filter((_, position) => position !== index))
  }

  const armSelect = (value: GuardrailArm, onChange: (arm: GuardrailArm) => void, label: string) => (
    <label className="phase2-field">{label}
      <select value={value} onChange={(event) => onChange(event.target.value as GuardrailArm)}>
        {GUARDRAIL_ARMS.map((info) => (
          <option
            key={info.id}
            value={info.id}
            disabled={!configured && info.requiresClassifier && info.id !== value}
            title={info.setupTitle}
          >
            {armLabel(info.id, configured)}
          </option>
        ))}
      </select>
    </label>
  )

  const modelSelect = (value: string, onChange: (next: string) => void, label: string) => (
    <label className="phase2-field">{label}
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        {(models.length ? models : [value || 'Loading models…']).map((id) => (
          <option value={id} key={id}>{id}</option>
        ))}
      </select>
    </label>
  )

  const send = async () => {
    const content = prompt.trim()
    if (!content || busy || variants.length < 2) return
    if (variants.some((variant) => !variant.model || (GUARDRAIL_ARMS.find((info) => info.id === variant.arm)?.requiresClassifier && !configured))) {
      setError('Choose a model for every variant, and configure the classifier before using classifier arms.')
      return
    }
    setBusy(true)
    setError(null)
    setResults([])
    try {
      const response = await getJson<CompareResponse>('/api/compare', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: [{ role: 'user', content }],
          model,
          guardrail_level: arm,
          variants: variants.map((variant) => ({ model: variant.model, guardrail_level: variant.arm })),
        }),
      })
      setResults(response.variants ?? [])
    } catch (sendError) {
      setError(sendError instanceof Error ? sendError.message : 'Could not run the comparison.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="phase2-page">
      <header className="phase2-topbar">
        <div className="breadcrumb"><span>Workspace</span><span className="breadcrumb-divider">/</span><strong>Compare</strong></div>
        <Button variant="outline" size="sm" onClick={onBackToChat}>
          <ArrowLeft size={14} /> Chat playground
        </Button>
      </header>
      <div className="phase2-scroll">
        <div className="phase2-content">
          <section className="phase2-heading">
            <div className="eyebrow">EXPLORATORY SIDE-BY-SIDE</div>
            <h1>Compare variants</h1>
            <p>Send the same prompt to 2–4 model / guardrail-arm combinations. These are exploratory requests and are never written into an evaluation run.</p>
          </section>

          {error && <div className="phase2-error" role="alert">{error}</div>}

          <div className="compare-layout">
            <div className="phase2-card compare-controls">
              <div className="phase2-section-title">
                <div className="phase2-section-icon"><GitCompareArrows size={17} /></div>
                <div><h2>Setup</h2><p>One prompt, several arms</p></div>
              </div>
              <label className="phase2-field">Prompt
                <textarea
                  value={prompt}
                  onChange={(event) => setPrompt(event.target.value)}
                  placeholder="Enter the prompt to send to every variant…"
                  aria-label="Comparison prompt"
                />
              </label>
              {modelSelect(model, setModel, 'Default model')}
              {armSelect(arm, setArm, 'Default arm')}
              <div className="compare-variants-heading">
                <span>VARIANTS ({variants.length}/4)</span>
                <button className="phase2-inline-button" onClick={addVariant} disabled={variants.length >= 4}>
                  <Plus size={12} /> Add variant
                </button>
              </div>
              {variants.map((variant, index) => (
                <div className="compare-variant" key={index}>
                  {modelSelect(variant.model, (next) => setVariant(index, { model: next }), `Model ${index + 1}`)}
                  {armSelect(variant.arm, (next) => setVariant(index, { arm: next }), 'Arm')}
                  <button
                    className="compare-variant-remove"
                    onClick={() => removeVariant(index)}
                    disabled={variants.length <= 2}
                    aria-label={`Remove variant ${index + 1}`}
                    title="Remove variant"
                  >
                    ×
                  </button>
                </div>
              ))}
              <p className="compare-estimate">
                <strong>{variants.length} model {variants.length === 1 ? 'call' : 'calls'}</strong> before sending — one per variant. Guardrail classifier arms may make extra classifier calls. No evaluation run is created.
              </p>
              {!configured && (
                <p className="phase2-note warning">
                  Classifier arms are disabled until {classifierBackend === 'cloudflare' ? 'Cloudflare Workers AI credentials' : 'the local classifier model'} are configured.
                </p>
              )}
              <Button className="phase2-primary" onClick={() => void send()} disabled={busy || !prompt.trim() || variants.length < 2}>
                {busy ? <LoaderCircle size={14} className="spin" /> : <Send size={14} />}
                {busy ? 'Running…' : 'Run comparison'}
              </Button>
            </div>

            <div>
              {results.length ? (
                <div className="compare-cards">
                  {results.map((result, index) => {
                    const verdict = guardrailObject(result.guardrail)
                    return (
                      <article className="compare-card" key={`${result.trace_id ?? 'variant'}-${index}`}>
                        <div className="compare-card-head">
                          <span className="compare-card-model">{result.model}</span>
                          <span className="phase2-badge">{verdict?.level ?? variants[index]?.arm ?? '—'}</span>
                        </div>
                        <div className="compare-answer">
                          <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]}>
                            {result.answer || 'Empty response'}
                          </ReactMarkdown>
                        </div>
                        <div className="compare-card-meta">
                          <span className="phase2-badge">{verdictOutcome(result.guardrail)}</span>
                          <span className="phase2-badge">{result.latency_ms.toFixed(1)} ms</span>
                          <span className={`phase2-badge ${result.cost?.priced ? 'good' : 'warn'}`}>{costLabel(result.cost)}</span>
                          {result.trace_id && (
                            <button className="phase2-inline-button" onClick={() => onOpenTrace(result.trace_id as string)}>
                              trace {shortId(result.trace_id)}
                            </button>
                          )}
                        </div>
                        {verdict?.stages?.map((stage, stageIndex) => (
                          <div className="compare-stage" key={`${stage.stage}-${stageIndex}`}>
                            <strong>{stage.stage}</strong> · {stage.outcome} · {stage.backend} · {stage.latency_ms.toFixed(1)} ms
                            {stage.findings?.length ? (
                              <div>{stage.findings.map((finding, findingIndex) => (
                                <span key={`${finding.scanner}-${findingIndex}`}>
                                  {finding.scanner}: {finding.outcome}{finding.category ? ` (${finding.category})` : ''}
                                  {findingIndex < (stage.findings?.length ?? 0) - 1 ? ' · ' : ''}
                                </span>
                              ))}</div>
                            ) : null}
                          </div>
                        ))}
                      </article>
                    )
                  })}
                </div>
              ) : (
                <div className="phase2-card trace-empty">
                  Variant answers will appear here after the comparison completes.
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
