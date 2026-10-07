import { lazy, Suspense, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { ArrowDown, ArrowUp, Bot, Check, CircleHelp, GitBranch, LoaderCircle, Plus, ShieldCheck, Sparkles } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import rehypeSanitize from 'rehype-sanitize'
import remarkGfm from 'remark-gfm'
import { Button } from './components/ui/button'
import './App.css'

const WORD_STREAM_DELAY_MS = 28
const EvaluationPage = lazy(() => import('./pages/EvaluationPage').then((module) => ({ default: module.EvaluationPage })))

type Message = {
  role: 'user' | 'assistant'
  content: string
  latencyMs?: number
  guardrail?: GuardrailVerdict
}

type GuardrailLevel = 'off' | 'scanners' | 'full'

type GuardrailVerdict = {
  level: GuardrailLevel
  outcome: 'passed' | 'redacted' | 'blocked'
  blocked: boolean
  blocked_by?: string | null
  category?: string | null
  latency_ms: number
}

type Health = {
  status: 'ok' | 'degraded'
  backend: string
  model: string
  llm_connected: boolean
  guardrail_level: GuardrailLevel
  guardrail_classifier_backend: 'cloudflare' | 'ollama'
  guardrail_classifier_configured: boolean
}

type ModelListResponse = { data: Array<{ id: string }> }

type ChatResponse = {
  answer: string
  model: string
  latency_ms: number
  guardrail: GuardrailVerdict
  sanitized_user_message: string | null
}

type ContextMessage = { role: 'user' | 'assistant'; content: string }

function newSessionId() {
  return globalThis.crypto?.randomUUID?.() ?? `session-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

const suggestions = [
  'Explain how prompt injection works',
  'Give me a Python example of an async API',
  'What should I measure when evaluating guardrails?',
]

function App() {
  const [messages, setMessages] = useState<Message[]>([])
  const [contextMessages, setContextMessages] = useState<ContextMessage[]>([])
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [streaming, setStreaming] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [health, setHealth] = useState<Health | null>(null)
  const [availableModels, setAvailableModels] = useState<string[]>([])
  const [modelOverride, setModelOverride] = useState<string | null>(null)
  const [guardrailOverride, setGuardrailOverride] = useState<GuardrailLevel | null>(null)
  const [activePage, setActivePage] = useState<'chat' | 'evaluation'>('chat')
  const [sessionId, setSessionId] = useState(newSessionId)
  const guardrailLevel = guardrailOverride ?? health?.guardrail_level ?? 'off'
  const selectedModel = availableModels.length
    ? modelOverride && availableModels.includes(modelOverride)
      ? modelOverride
      : availableModels.includes(health?.model ?? '')
        ? health?.model ?? availableModels[0]
        : availableModels[0]
    : modelOverride ?? health?.model ?? ''
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let active = true
    const checkHealth = async () => {
      try {
        const response = await fetch('/health')
        if (!response.ok) throw new Error('Health check failed')
        const data: Health = await response.json()
        if (active) setHealth(data)
      } catch {
        if (active) setHealth(null)
      }
    }
    void checkHealth()
    const timer = window.setInterval(checkHealth, 30_000)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [])

  useEffect(() => {
    let active = true
    const loadModels = async () => {
      try {
        const response = await fetch('/v1/models')
        if (!response.ok) throw new Error('Model list is unavailable')
        const result: ModelListResponse = await response.json()
        if (active) setAvailableModels(result.data.map((model) => model.id))
      } catch {
        if (active && health?.model) setAvailableModels([health.model])
      }
    }
    void loadModels()
    return () => { active = false }
  }, [health?.model])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: busy ? 'auto' : 'smooth', block: 'end' })
  }, [messages, busy])

  const sendMessage = async (text: string) => {
    const content = text.trim()
    if (!content || busy) return

    const nextMessages: Message[] = [...messages, { role: 'user', content }]
    const requestMessages: ContextMessage[] = [...contextMessages, { role: 'user', content }]
    setMessages(nextMessages)
    setDraft('')
    setBusy(true)
    setStreaming(false)
    setError(null)

    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'x-opencode-session': sessionId,
        },
        body: JSON.stringify({
          messages: requestMessages,
          model: selectedModel || undefined,
          guardrail_level: guardrailLevel,
        }),
      })
      const data = (await response.json()) as ChatResponse | { detail?: string }
      if (!response.ok) {
        const detail = 'detail' in data ? data.detail : undefined
        throw new Error(detail || `The request failed (${response.status}).`)
      }
      const result = data as ChatResponse
      const answer = result.answer || 'The model returned an empty response.'
      if (result.sanitized_user_message !== null) {
        setContextMessages([
          ...requestMessages,
          { role: 'assistant', content: answer },
        ])
      }
      const assistantMessage: Message = {
        role: 'assistant',
        content: '',
        latencyMs: result.latency_ms,
        guardrail: result.guardrail,
      }
      const wordChunks = answer.match(/\S+\s*/g) ?? [answer]
      let visibleAnswer = ''

      setStreaming(true)
      setMessages([...nextMessages, assistantMessage])
      for (const word of wordChunks) {
        visibleAnswer += word
        setMessages([...nextMessages, { ...assistantMessage, content: visibleAnswer }])
        await new Promise((resolve) => window.setTimeout(resolve, WORD_STREAM_DELAY_MS))
      }
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not reach the chat service.')
    } finally {
      setStreaming(false)
      setBusy(false)
      textareaRef.current?.focus()
    }
  }

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    void sendMessage(draft)
  }

  const onComposerKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      void sendMessage(draft)
    }
  }

  const startNewChat = () => {
    if (!busy) {
      setMessages([])
      setContextMessages([])
      setDraft('')
      setError(null)
      setSessionId(newSessionId())
      setGuardrailOverride(null)
      textareaRef.current?.focus()
    }
  }

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="/" aria-label="Guardrail Lab home">
          <span className="brand-mark"><ShieldCheck size={19} strokeWidth={2.2} /></span>
          <span>guardrail<span className="brand-light">.lab</span></span>
        </a>

        <Button className="new-chat" variant="outline" onClick={startNewChat}>
          <Plus size={17} /> New chat
        </Button>

        <div className="sidebar-label">WORKSPACE</div>
        <button
          className={`nav-item ${activePage === 'chat' ? 'active' : ''}`}
          onClick={() => setActivePage('chat')}
        >
          <Bot size={17} /><span>Chat playground</span>
        </button>
        <button
          className={`nav-item ${activePage === 'evaluation' ? 'active' : ''}`}
          onClick={() => setActivePage('evaluation')}
        >
          <ShieldCheck size={17} /><span>Security evaluation</span>
        </button>

        <div className="sidebar-bottom">
          <div className="model-card">
            <div className="model-card-top">
              <span className={`status-dot ${health?.llm_connected ? 'online' : ''}`} />
              <span>{health?.llm_connected ? 'Model online' : 'Model offline'}</span>
              <CircleHelp size={14} className="help-icon" />
            </div>
            <div className="model-name">{selectedModel || 'Loading model list…'}</div>
          </div>
          <a className="github-link" href="https://github.com/sjreal/idt" target="_blank" rel="noreferrer">
            <GitBranch size={16} /> Project repository <ArrowDown size={13} className="external-arrow" />
          </a>
        </div>
      </aside>

      {activePage === 'chat' ? <section className="main-panel">
        <header className="topbar">
          <div className="breadcrumb"><span>Playground</span><span className="breadcrumb-divider">/</span><strong>Chat</strong></div>
          <div className="topbar-right">
            <div className="active-model" title={`Current model: ${selectedModel || 'not detected'}`}>
              <span className="active-model-label">MODEL</span>
              <select
                className="active-model-select"
                aria-label="Generation model"
                value={selectedModel}
                onChange={(event) => setModelOverride(event.target.value)}
                disabled={!availableModels.length}
              >
                {(availableModels.length ? availableModels : [health?.model ?? 'Loading models…']).map((model) => (
                  <option key={model} value={model}>{model}</option>
                ))}
              </select>
            </div>
            <label className="guardrail-mode" title="Select the checks applied to each message">
              <span>GUARDRAILS</span>
              <select
                value={guardrailLevel}
                onChange={(event) => setGuardrailOverride(event.target.value as GuardrailLevel)}
                aria-label="Guardrail mode"
              >
                <option value="off">Off</option>
                <option value="scanners">Scanners</option>
                <option
                  value="full"
                  disabled={!health?.guardrail_classifier_configured && guardrailLevel !== 'full'}
                  title={health?.guardrail_classifier_backend === 'cloudflare'
                    ? 'Full mode needs Cloudflare Workers AI credentials.'
                    : 'Full mode requires the local llama-guard3:1b model.'}
                >
                  {health?.guardrail_classifier_configured ? 'Full' : 'Full · setup needed'}
                </option>
              </select>
            </label>
            <div className={`connection-status ${health?.llm_connected ? 'connected' : ''}`}>
              <span className="status-dot" />
              {health?.llm_connected ? 'Connected' : 'Waiting for model'}
            </div>
            <div className="api-label">LOCAL API</div>
          </div>
        </header>

        <div className={`conversation ${messages.length ? 'has-messages' : ''}`}>
          {messages.length === 0 ? (
            <div className="welcome">
              <div className="welcome-icon"><Sparkles size={22} /></div>
              <div className="eyebrow">A PLAYGROUND FOR AI SAFETY</div>
              <h1>What would you<br />like to <span>explore?</span></h1>
              <p className="welcome-copy">Explore model behavior and test AI safety.<br />Start a conversation to try it out.</p>
              <div className="suggestions-label">TRY ASKING</div>
              <div className="suggestions">
                {suggestions.map((suggestion) => (
                  <button className="suggestion" key={suggestion} onClick={() => void sendMessage(suggestion)}>
                    <span>{suggestion}</span><ArrowUp size={14} />
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="message-list">
              {messages.map((message, index) => (
                <article className={`message-row ${message.role}`} key={`${message.role}-${index}`}>
                  <div className={`avatar ${message.role}`}>
                      {message.role === 'assistant' ? <Bot size={17} /> : 'S'}
                  </div>
                  <div className="message-content">
                    <div className="message-meta">
                      <strong>{message.role === 'assistant' ? 'Guardrail Lab' : 'You'}</strong>
                      {message.latencyMs !== undefined && <span>{(message.latencyMs / 1000).toFixed(2)}s</span>}
                    </div>
                    {message.guardrail && (
                      <div className={`guardrail-verdict ${message.guardrail.outcome}`}>
                        <ShieldCheck size={13} />
                        <span>
                          {message.guardrail.outcome === 'blocked'
                            ? `Blocked · ${message.guardrail.blocked_by ?? 'safety check'}`
                            : message.guardrail.outcome === 'redacted'
                              ? 'Sensitive value redacted'
                              : message.guardrail.level === 'off'
                                ? 'Baseline · guardrails off'
                                : `${message.guardrail.level} checks passed`}
                        </span>
                        <span className="guardrail-latency">{message.guardrail.latency_ms} ms</span>
                        {message.guardrail.category && <span className="guardrail-category">{message.guardrail.category}</span>}
                      </div>
                    )}
                    {message.role === 'assistant' ? (
                      <div className="markdown-body">
                        <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]}>
                          {message.content}
                        </ReactMarkdown>
                        {streaming && index === messages.length - 1 && <span className="stream-caret" aria-hidden="true" />}
                      </div>
                    ) : (
                      <p>{message.content}</p>
                    )}
                  </div>
                </article>
              ))}
              {busy && !streaming && (
                <article className="message-row assistant">
                  <div className="avatar assistant"><Bot size={17} /></div>
                  <div className="message-content">
                    <div className="message-meta"><strong>Guardrail Lab</strong></div>
                    <div className="thinking"><LoaderCircle size={15} /> Waiting for the model…</div>
                  </div>
                </article>
              )}
              <div ref={endRef} />
            </div>
          )}
        </div>

        <div className="composer-wrap">
          {error && <div className="error-banner" role="alert">{error}</div>}
          {!health?.llm_connected && messages.length === 0 && (
            <div className="setup-hint"><span className="hint-icon"><Check size={13} /></span> {health?.backend === 'opencode-go' ? 'Configure OPENCODE_GO_API_KEY to connect your Go account.' : 'Start Ollama and pull the configured model to begin chatting.'}</div>
          )}
          {guardrailLevel === 'full' && !health?.guardrail_classifier_configured && messages.length === 0 && (
            <div className="setup-hint guardrail-hint">
              <span className="hint-icon"><Check size={13} /></span>
              {health?.guardrail_classifier_backend === 'cloudflare'
                ? 'Configure CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN in .env.'
                : <>Enable the Ollama profile, then run <code>make guard-model-pull</code>.</>}
            </div>
          )}
          <form className="composer" onSubmit={onSubmit}>
            <textarea
              ref={textareaRef}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={onComposerKeyDown}
              placeholder="Message Guardrail Lab…"
              rows={1}
              aria-label="Message"
              disabled={busy}
            />
            <div className="composer-footer">
              <span className="composer-note"><span className="privacy-dot" /> {health?.backend === 'opencode-go' ? 'Using OpenCode Go' : 'Runs on your machine'}</span>
              <div className="composer-actions">
                <span className="keyboard-hint">↵ to send</span>
                <Button className="send-button" size="icon" type="submit" disabled={!draft.trim() || busy} aria-label="Send message">
                  {busy ? <LoaderCircle size={16} className="spin" /> : <ArrowUp size={17} />}
                </Button>
              </div>
            </div>
          </form>
          <div className="disclaimer">AI responses can be inaccurate. Requests are sent to the configured model provider.</div>
        </div>
      </section> : <section className="main-panel"><Suspense fallback={<div className="evaluation-loading">Loading evaluation dashboard…</div>}><EvaluationPage health={health} generationModel={selectedModel} availableModels={availableModels} onGenerationModelChange={setModelOverride} onBackToChat={() => setActivePage('chat')} /></Suspense></section>}
    </main>
  )
}

export default App
