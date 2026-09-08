import { useState, useEffect, useCallback } from 'react'

const API = import.meta.env.VITE_API_URL || ''

function useFetch(url, interval = null) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const load = useCallback(() => {
    fetch(`${API}${url}`).then(r => r.json()).then(setData).catch(setErr)
  }, [url])
  useEffect(() => {
    load()
    if (interval) { const id = setInterval(load, interval); return () => clearInterval(id) }
  }, [load, interval])
  return { data, err, reload: load }
}

function post(url, body = {}) {
  return fetch(`${API}${url}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(r => r.json())
}

// ── Login Screen ──
function LoginScreen({ onLogin }) {
  const [u, setU] = useState('')
  const [p, setP] = useState('')
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(false)

  const submit = async () => {
    setLoading(true); setErr('')
    try {
      const r = await post('/api/login', { username: u, password: p })
      if (r.status === 'ok') onLogin(r.balance)
      else setErr(r.message || 'Login failed')
    } catch (e) { setErr(e.message) }
    setLoading(false)
  }

  return (
    <div className="login-screen">
      <div className="login-box">
        <h2>Chimera <span style={{ color: 'var(--gold)' }}>Scalping</span> Engine</h2>
        <input placeholder="Betfair Username" value={u} onChange={e => setU(e.target.value)} />
        <input placeholder="Password" type="password" value={p} onChange={e => setP(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && submit()} />
        {err && <p style={{ color: 'var(--red)', fontSize: 12, marginBottom: 12 }}>{err}</p>}
        <button className="btn btn-gold" style={{ width: '100%' }} onClick={submit} disabled={loading}>
          {loading ? 'Authenticating...' : 'Login'}
        </button>
      </div>
    </div>
  )
}

// ── Dashboard Tab ──
function DashboardTab() {
  const { data: state } = useFetch('/api/state', 5000)
  const { data: risk } = useFetch('/api/risk', 10000)

  if (!state) return <div className="empty"><p>Loading...</p></div>

  const s = state
  const pf = risk?.portfolio || {}

  return (
    <div>
      <div className="grid-4" style={{ marginBottom: 24 }}>
        <div className="card">
          <div className="stat">
            <div className="stat-value" style={{ color: 'var(--gold)' }}>{s.active_trades}</div>
            <div className="stat-label">Active Trades</div>
          </div>
        </div>
        <div className="card">
          <div className="stat">
            <div className="stat-value">{s.active_alerts}</div>
            <div className="stat-label">Active Alerts</div>
          </div>
        </div>
        <div className="card">
          <div className="stat">
            <div className="stat-value">£{s.balance?.toFixed(2) || '—'}</div>
            <div className="stat-label">Balance</div>
          </div>
        </div>
        <div className="card">
          <div className="stat">
            <div className="stat-value">{s.markets_count}</div>
            <div className="stat-label">Markets</div>
          </div>
        </div>
      </div>

      <div className="grid-2">
        <div className="card">
          <h3>Engine Status</h3>
          <table>
            <tbody>
              <tr><td>Status</td><td><span className={`badge ${s.status === 'RUNNING' ? 'badge-green' : 'badge-amber'}`}>{s.status}</span></td></tr>
              <tr><td>Mode</td><td><span className={`badge ${s.dry_run ? 'badge-amber' : 'badge-red'}`}>{s.dry_run ? 'DRY RUN' : 'LIVE'}</span></td></tr>
              <tr><td>Countries</td><td style={{ fontFamily: 'var(--font-mono)' }}>{s.countries?.join(', ')}</td></tr>
              <tr><td>Window</td><td style={{ fontFamily: 'var(--font-mono)' }}>{s.process_window}m</td></tr>
              <tr><td>Point Value</td><td style={{ fontFamily: 'var(--font-mono)' }}>£{s.point_value}</td></tr>
              <tr><td>Ladder</td><td style={{ fontFamily: 'var(--font-mono)' }}>{s.ladder_profile}</td></tr>
              <tr><td>Last Scan</td><td style={{ fontFamily: 'var(--font-mono)', fontSize: 11 }}>{s.last_scan?.slice(11, 19) || '—'}</td></tr>
            </tbody>
          </table>
        </div>

        <div className="card">
          <h3>Risk & Portfolio</h3>
          <table>
            <tbody>
              <tr><td>Open Trades</td><td style={{ fontFamily: 'var(--font-mono)' }}>{pf.total_open_trades || 0}</td></tr>
              <tr><td>Total Exposure</td><td style={{ fontFamily: 'var(--font-mono)' }}>£{pf.total_exposure?.toFixed(2) || '0.00'}</td></tr>
              <tr><td>Daily P&L</td><td style={{ fontFamily: 'var(--font-mono)', color: (pf.daily_realised_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>
                £{(pf.daily_realised_pnl || 0).toFixed(2)}
              </td></tr>
              <tr><td>Kill Switch</td><td>
                <span className={`badge ${risk?.config?.kill_switch_enabled ? 'badge-red' : 'badge-green'}`}>
                  {risk?.config?.kill_switch_enabled ? 'ACTIVE' : 'OFF'}
                </span>
              </td></tr>
              <tr><td>Daily Cap</td><td style={{ fontFamily: 'var(--font-mono)' }}>£{risk?.config?.level_4_portfolio?.daily_drawdown_cap || '—'}</td></tr>
              <tr><td>Bankroll</td><td style={{ fontFamily: 'var(--font-mono)' }}>£{risk?.config?.level_4_portfolio?.bankroll || '—'}</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      {s.errors?.length > 0 && (
        <div className="card" style={{ borderColor: 'rgba(231,76,60,0.3)' }}>
          <h3 style={{ color: 'var(--red)' }}>Errors</h3>
          {s.errors.map((e, i) => (
            <div key={i} style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--text-secondary)', marginBottom: 4 }}>
              {e.timestamp?.slice(11, 19)} — {e.message}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ── Trades Tab ──
function TradesTab() {
  const { data, reload } = useFetch('/api/trades', 5000)

  const trades = data?.trades ? Object.values(data.trades) : []

  const doControl = async (tid, mode) => {
    await post(`/api/trades/${tid}/control`, { mode, reason: 'UI action' })
    reload()
  }
  const doFlatten = async (tid) => {
    if (!confirm('Flatten this trade? This will close all positions.')) return
    await post(`/api/trades/${tid}/flatten`)
    reload()
  }

  if (!trades.length) return <div className="empty"><p>No active trades</p></div>

  return (
    <div>
      {trades.map(t => (
        <div key={t.trade_id} className="card">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
            <div>
              <span style={{ fontSize: 16, fontWeight: 700 }}>{t.horse_name}</span>
              <span style={{ color: 'var(--text-muted)', marginLeft: 12, fontSize: 12 }}>{t.venue}</span>
              <span style={{ color: 'var(--text-muted)', marginLeft: 8, fontSize: 12, fontFamily: 'var(--font-mono)' }}>
                {t.race_time?.slice(11, 16)}
              </span>
            </div>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <span className={`badge ${t.state === 'PLANNED' ? 'badge-blue' : t.state === 'LIVE_EXPOSED' ? 'badge-amber' : t.state === 'HEDGED' ? 'badge-green' : 'badge-gold'}`}>
                {t.state}
              </span>
              <span className={`badge ${t.control_mode === 'AUTO' ? 'badge-green' : t.control_mode === 'MANUAL_LOCK' ? 'badge-red' : 'badge-amber'}`}>
                {t.control_mode}
              </span>
            </div>
          </div>

          <div className="grid-4" style={{ marginBottom: 12 }}>
            <div><span style={{ color: 'var(--text-muted)', fontSize: 11 }}>Entry Odds</span><br /><span style={{ fontFamily: 'var(--font-mono)' }}>{t.entry_odds?.toFixed(2)}</span></div>
            <div><span style={{ color: 'var(--text-muted)', fontSize: 11 }}>Quality</span><br /><span style={{ fontFamily: 'var(--font-mono)' }}>{t.quality_score?.toFixed(4)}</span></div>
            <div><span style={{ color: 'var(--text-muted)', fontSize: 11 }}>Trade ID</span><br /><span style={{ fontFamily: 'var(--font-mono)', fontSize: 11 }}>{t.trade_id}</span></div>
            <div><span style={{ color: 'var(--text-muted)', fontSize: 11 }}>Created</span><br /><span style={{ fontFamily: 'var(--font-mono)', fontSize: 11 }}>{t.created_at?.slice(11, 19)}</span></div>
          </div>

          <div style={{ display: 'flex', gap: 8 }}>
            {t.control_mode !== 'AUTO' && <button className="btn btn-sm btn-green" onClick={() => doControl(t.trade_id, 'AUTO')}>Auto</button>}
            {t.control_mode !== 'ASSISTED' && <button className="btn btn-sm" onClick={() => doControl(t.trade_id, 'ASSISTED')}>Assisted</button>}
            {t.control_mode !== 'MANUAL_LOCK' && <button className="btn btn-sm btn-red" onClick={() => doControl(t.trade_id, 'MANUAL_LOCK')}>Lock</button>}
            <button className="btn btn-sm btn-red" onClick={() => doFlatten(t.trade_id)}>Flatten</button>
          </div>
        </div>
      ))}
    </div>
  )
}

// ── Alerts Tab (Part C) ──
function AlertsTab() {
  const { data, reload } = useFetch('/api/alerts', 3000)
  const [confirmId, setConfirmId] = useState(null)
  const [stake, setStake] = useState('')
  const [odds, setOdds] = useState('')

  const alerts = data?.active || []

  const doConfirm = async (alertId) => {
    await post(`/api/alerts/${alertId}/confirm`, {
      alert_id: alertId, stake: parseFloat(stake), odds: parseFloat(odds),
    })
    setConfirmId(null); setStake(''); setOdds('')
    reload()
  }

  const doDismiss = async (alertId) => {
    await post(`/api/alerts/${alertId}/dismiss`)
    reload()
  }

  if (!alerts.length) return (
    <div className="empty">
      <p>No active bookmaker trigger alerts</p>
      <p style={{ marginTop: 8, fontSize: 11 }}>Alerts appear when bookmaker/exchange spread conditions are met</p>
    </div>
  )

  return (
    <div>
      {alerts.map(a => (
        <div key={a.alert_id} className={`alert-card ${a.confidence_band === 'PRIORITY' ? 'priority' : a.confidence_band === 'HIGH' ? 'high' : ''}`}>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
            <div>
              <span style={{ fontSize: 16, fontWeight: 700 }}>{a.runner_name}</span>
              <span className={`badge badge-gold`} style={{ marginLeft: 8 }}>Family {a.trigger_family}</span>
              <span className={`badge ${a.confidence_band === 'PRIORITY' ? 'badge-red' : a.confidence_band === 'HIGH' ? 'badge-amber' : 'badge-blue'}`} style={{ marginLeft: 4 }}>
                {a.confidence_band}
              </span>
            </div>
            <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--text-muted)' }}>
              {a.recommended_action}
            </span>
          </div>

          <div className="grid-4" style={{ marginBottom: 12, fontSize: 12 }}>
            <div><span style={{ color: 'var(--text-muted)' }}>Bookmaker</span><br />{a.bookmaker} @ <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--green)' }}>{a.bookmaker_price?.toFixed(2)}</span></div>
            <div><span style={{ color: 'var(--text-muted)' }}>Exch Back</span><br /><span style={{ fontFamily: 'var(--font-mono)' }}>{a.exchange_back?.toFixed(2)}</span></div>
            <div><span style={{ color: 'var(--text-muted)' }}>Spread</span><br /><span style={{ fontFamily: 'var(--font-mono)' }}>{(a.relative_spread_pct * 100).toFixed(2)}%</span></div>
            <div><span style={{ color: 'var(--text-muted)' }}>Score</span><br /><span style={{ fontFamily: 'var(--font-mono)', color: 'var(--gold)' }}>{a.score?.toFixed(4)}</span></div>
          </div>

          {confirmId === a.alert_id ? (
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <input placeholder="Stake" value={stake} onChange={e => setStake(e.target.value)} style={{ width: 80 }} />
              <input placeholder="Odds" value={odds} onChange={e => setOdds(e.target.value)} style={{ width: 80 }} />
              <button className="btn btn-sm btn-green" onClick={() => doConfirm(a.alert_id)}>Confirm</button>
              <button className="btn btn-sm" onClick={() => setConfirmId(null)}>Cancel</button>
            </div>
          ) : (
            <div style={{ display: 'flex', gap: 8 }}>
              <button className="btn btn-sm btn-gold" onClick={() => setConfirmId(a.alert_id)}>Confirm Bet</button>
              <button className="btn btn-sm" onClick={() => doDismiss(a.alert_id)}>Dismiss</button>
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

// ── Markets Tab ──
function MarketsTab() {
  const { data } = useFetch('/api/markets', 10000)
  const markets = data?.markets || []

  return (
    <div className="card">
      <h3>Markets ({markets.length})</h3>
      <table>
        <thead>
          <tr><th>Time</th><th>Venue</th><th>Race</th><th>Runners</th><th>Mins to Off</th></tr>
        </thead>
        <tbody>
          {markets.slice(0, 40).map(m => (
            <tr key={m.market_id}>
              <td>{m.race_time?.slice(11, 16)}</td>
              <td>{m.venue}</td>
              <td>{m.market_name}</td>
              <td>{m.runners?.length || '?'}</td>
              <td>
                <span className={`badge ${m.minutes_to_off <= 15 ? 'badge-green' : m.minutes_to_off <= 60 ? 'badge-amber' : 'badge-blue'}`}>
                  {m.minutes_to_off?.toFixed(0)}m
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ── Risk Tab ──
function RiskTab() {
  const { data, reload } = useFetch('/api/risk', 10000)

  const toggleKill = async () => {
    await post('/api/risk/kill-switch')
    reload()
  }

  if (!data) return <div className="empty"><p>Loading...</p></div>

  const c = data.config || {}
  const l1 = c.level_1_trade || {}
  const l2 = c.level_2_market || {}
  const l4 = c.level_4_portfolio || {}

  return (
    <div>
      <div style={{ marginBottom: 16 }}>
        <button className={`btn ${c.kill_switch_enabled ? 'btn-green' : 'btn-red'}`} onClick={toggleKill}>
          {c.kill_switch_enabled ? 'Deactivate Kill Switch' : 'Activate Kill Switch'}
        </button>
        {c.kill_switch_enabled && <span style={{ color: 'var(--red)', marginLeft: 12, fontWeight: 700 }}>KILL SWITCH ACTIVE — ALL NEW TRADES BLOCKED</span>}
      </div>

      <div className="grid-3">
        <div className="card">
          <h3>Level 1 — Trade Risk</h3>
          <table><tbody>
            <tr><td>Max Loss/Trade</td><td style={{ fontFamily: 'var(--font-mono)' }}>£{l1.max_loss_per_trade}</td></tr>
            <tr><td>Max Unhedged</td><td style={{ fontFamily: 'var(--font-mono)' }}>£{l1.max_unhedged_loss}</td></tr>
            <tr><td>Max Liability</td><td style={{ fontFamily: 'var(--font-mono)' }}>£{l1.max_liability_per_trade}</td></tr>
            <tr><td>Min 1st Rung P&L</td><td style={{ fontFamily: 'var(--font-mono)' }}>£{l1.min_first_rung_only_pnl}</td></tr>
          </tbody></table>
        </div>

        <div className="card">
          <h3>Level 2 — Market Risk</h3>
          <table><tbody>
            <tr><td>Max/Market</td><td style={{ fontFamily: 'var(--font-mono)' }}>£{l2.max_exposure_per_market}</td></tr>
            <tr><td>Max Trades/Market</td><td style={{ fontFamily: 'var(--font-mono)' }}>{l2.max_simultaneous_trades_per_market}</td></tr>
            <tr><td>Max Correlated</td><td style={{ fontFamily: 'var(--font-mono)' }}>£{l2.max_correlated_exposure}</td></tr>
          </tbody></table>
        </div>

        <div className="card">
          <h3>Level 4 — Portfolio</h3>
          <table><tbody>
            <tr><td>Daily Cap</td><td style={{ fontFamily: 'var(--font-mono)' }}>£{l4.daily_drawdown_cap}</td></tr>
            <tr><td>Rolling Cap</td><td style={{ fontFamily: 'var(--font-mono)' }}>£{l4.rolling_loss_cap}</td></tr>
            <tr><td>Bankroll</td><td style={{ fontFamily: 'var(--font-mono)' }}>£{l4.bankroll}</td></tr>
            <tr><td>Capital Cap</td><td style={{ fontFamily: 'var(--font-mono)' }}>{(l4.capital_utilisation_cap_pct * 100)}%</td></tr>
          </tbody></table>
        </div>
      </div>

      <div className="card">
        <h3>Trade Gate Thresholds</h3>
        <div className="grid-3">
          <div><span style={{ color: 'var(--text-muted)', fontSize: 11 }}>Max 1st-Lay-Only Loss</span><br /><span style={{ fontFamily: 'var(--font-mono)' }}>£{data.trade_gate?.max_first_lay_only_loss}</span></div>
          <div><span style={{ color: 'var(--text-muted)', fontSize: 11 }}>Max No-Lay Loss</span><br /><span style={{ fontFamily: 'var(--font-mono)' }}>£{data.trade_gate?.max_no_lay_loss}</span></div>
          <div><span style={{ color: 'var(--text-muted)', fontSize: 11 }}>Max Stop Loss</span><br /><span style={{ fontFamily: 'var(--font-mono)' }}>£{data.trade_gate?.max_stop_loss}</span></div>
        </div>
      </div>
    </div>
  )
}

// ── Audit Tab ──
function AuditTab() {
  const { data } = useFetch('/api/audit', 10000)
  const transitions = data?.transitions || []

  return (
    <div className="card">
      <h3>State Transitions</h3>
      {transitions.length === 0 ? <div className="empty"><p>No transitions recorded</p></div> : (
        <table>
          <thead><tr><th>Time</th><th>Type</th><th>ID</th><th>From</th><th>To</th><th>Reason</th><th>User</th></tr></thead>
          <tbody>
            {[...transitions].reverse().slice(0, 50).map((t, i) => (
              <tr key={i}>
                <td>{t.timestamp?.slice(11, 19)}</td>
                <td><span className="badge badge-blue">{t.entity_type}</span></td>
                <td style={{ fontSize: 10 }}>{t.entity_id?.slice(0, 12)}</td>
                <td>{t.from_state}</td>
                <td>{t.to_state}</td>
                <td style={{ maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.reason}</td>
                <td>{t.user_id || 'system'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

// ── Controls Bar ──
function ControlsBar({ state, onReload }) {
  const doStart = () => post('/api/engine/start').then(onReload)
  const doStop = () => post('/api/engine/stop').then(onReload)
  const doDryRun = () => post('/api/engine/dry-run').then(onReload)

  return (
    <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
      {state?.status === 'RUNNING' ? (
        <button className="btn btn-red btn-sm" onClick={doStop}>Stop</button>
      ) : (
        <button className="btn btn-green btn-sm" onClick={doStart}>Start</button>
      )}
      <button className={`btn btn-sm ${state?.dry_run ? '' : 'btn-red'}`} onClick={doDryRun}>
        {state?.dry_run ? 'DRY RUN' : 'LIVE'}
      </button>
    </div>
  )
}

// ── Main App ──
export default function App() {
  const [authed, setAuthed] = useState(false)
  const [authChecked, setAuthChecked] = useState(false)
  const [tab, setTab] = useState('dashboard')
  const { data: state, reload } = useFetch('/api/state', 5000)

  // Ask the backend whether a Betfair session actually exists.
  // /api/state is public and always returns a status field, so it can never
  // be used as an auth signal — /api/keepalive reports the real thing.
  useEffect(() => {
    const check = () =>
      fetch(`${API}/api/keepalive`)
        .then(r => r.json())
        .then(d => setAuthed(!!d.authenticated))
        .catch(() => {})
        .finally(() => setAuthChecked(true))
    check()
    const id = setInterval(check, 10000)
    return () => clearInterval(id)
  }, [])

  if (!authChecked) return null
  if (!authed) return <LoginScreen onLogin={() => setAuthed(true)} />

  const tabs = [
    { id: 'dashboard', label: 'Dashboard' },
    { id: 'trades', label: 'Trades' },
    { id: 'alerts', label: 'Triggers' },
    { id: 'markets', label: 'Markets' },
    { id: 'risk', label: 'Risk' },
    { id: 'audit', label: 'Audit' },
  ]

  return (
    <div className="app">
      <div className="header">
        <h1>Chimera <span>Scalping</span></h1>
        <div className="header-status">
          <span>
            <span className={`badge ${state?.status === 'RUNNING' ? 'badge-green' : 'badge-amber'}`}>{state?.status || '?'}</span>
          </span>
          <span style={{ color: 'var(--text-muted)' }}>
            {state?.active_trades || 0} trades · {state?.active_alerts || 0} alerts
          </span>
          <ControlsBar state={state} onReload={reload} />
          <button className="btn btn-sm" onClick={() => { post('/api/logout'); setAuthed(false) }}>Logout</button>
        </div>
      </div>

      <div className="nav">
        {tabs.map(t => (
          <button key={t.id} className={tab === t.id ? 'active' : ''} onClick={() => setTab(t.id)}>
            {t.label}
          </button>
        ))}
      </div>

      <div className="content">
        {tab === 'dashboard' && <DashboardTab />}
        {tab === 'trades' && <TradesTab />}
        {tab === 'alerts' && <AlertsTab />}
        {tab === 'markets' && <MarketsTab />}
        {tab === 'risk' && <RiskTab />}
        {tab === 'audit' && <AuditTab />}
      </div>
    </div>
  )
}
