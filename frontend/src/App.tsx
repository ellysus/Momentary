import './App.css'
import { useEffect, useMemo, useState } from 'react'

type Me = {
  user_id: number
  username: string
}

type PromptStatus = {
  active: boolean
  lastPrompt: string | null
  expiresAt: string | null
  secondsRemaining: number
}

type VapidKeyResponse = {
  publicKey: string
}

type PushSubscriptionJSON = {
  endpoint: string
  keys: { p256dh: string; auth: string }
}

function toUint8Array(base64Url: string): Uint8Array<ArrayBuffer> {
  const padding = '='.repeat((4 - (base64Url.length % 4)) % 4)
  const base64 = (base64Url + padding).replace(/-/g, '+').replace(/_/g, '/')
  const raw = atob(base64)
  const output: Uint8Array<ArrayBuffer> = new Uint8Array(new ArrayBuffer(raw.length))
  for (let i = 0; i < raw.length; i++) output[i] = raw.charCodeAt(i)
  return output
}

function App() {
  const apiBase = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.trim() || ''

  const [me, setMe] = useState<Me | null>(null)
  const [prompt, setPrompt] = useState<PromptStatus | null>(null)
  const [authError, setAuthError] = useState<string | null>(null)
  const [info, setInfo] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [loginUsername, setLoginUsername] = useState('')
  const [loginPassword, setLoginPassword] = useState('')
  const [regUsername, setRegUsername] = useState('')
  const [regPassword, setRegPassword] = useState('')

  const statusText = useMemo(() => {
    if (!me) return null
    return `Logged in as ${me.username} (ID: ${me.user_id})`
  }, [me])

  async function apiFetch(path: string, init?: RequestInit) {
    const url = apiBase ? `${apiBase}${path}` : path
    return fetch(url, { credentials: 'include', ...init })
  }

  async function refreshMe() {
    try {
      const res = await apiFetch('/me')
      if (!res.ok) throw new Error('not logged in')
      const data = (await res.json()) as Me
      setMe(data)
    } catch {
      setMe(null)
    }
  }

  async function refreshPrompt() {
    try {
      const res = await apiFetch('/prompt/status')
      if (!res.ok) return
      const data = (await res.json()) as PromptStatus
      setPrompt(data)
    } catch {
      // ignore
    }
  }

  useEffect(() => {
    refreshMe()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!me) return
    refreshPrompt()
    const t = window.setInterval(() => refreshPrompt(), 2500)
    return () => window.clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [me?.user_id])

  async function doRegister() {
    setAuthError(null)
    setInfo(null)
    setBusy(true)
    try {
      const res = await apiFetch('/auth/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: regUsername, password: regPassword }),
      })
      if (!res.ok) {
        const msg = await res.text()
        throw new Error(msg || 'Registration failed')
      }
      const data = (await res.json()) as Me
      setMe(data)
      setInfo('Registered and logged in.')
    } catch (e) {
      setAuthError(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function doLogin() {
    setAuthError(null)
    setInfo(null)
    setBusy(true)
    try {
      const res = await apiFetch('/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: loginUsername, password: loginPassword }),
      })
      if (!res.ok) {
        const msg = await res.text()
        throw new Error(msg || 'Login failed')
      }
      const data = (await res.json()) as Me
      setMe(data)
      setInfo('Logged in.')
    } catch (e) {
      setAuthError(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function doLogout() {
    setInfo(null)
    setAuthError(null)
    setBusy(true)
    try {
      await apiFetch('/auth/logout', { method: 'POST' })
      setMe(null)
      setPrompt(null)
    } finally {
      setBusy(false)
    }
  }

  const pushSupported =
    'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window

  async function enablePush() {
    setInfo(null)
    setAuthError(null)
    if (!pushSupported) {
      setAuthError('Push is not supported in this browser.')
      return
    }
    try {
      const permission = await Notification.requestPermission()
      if (permission !== 'granted') {
        setAuthError('Notification permission not granted.')
        return
      }

      const reg = await navigator.serviceWorker.register('/sw.js')
      const keyRes = await apiFetch('/push/vapid-public-key')
      if (!keyRes.ok) throw new Error('Missing VAPID public key on backend.')
      const keyJson = (await keyRes.json()) as VapidKeyResponse
      const appServerKey = toUint8Array(keyJson.publicKey)

      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: appServerKey,
      })

      const json = sub.toJSON() as PushSubscriptionJSON
      const saveRes = await apiFetch('/push/subscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(json),
      })
      if (!saveRes.ok) throw new Error('Failed to save push subscription.')

      setInfo('Push notifications enabled.')
    } catch (e) {
      setAuthError(String(e))
    }
  }

  async function sendTestPrompt() {
    setInfo(null)
    setAuthError(null)
    setBusy(true)
    try {
      const res = await apiFetch('/admin/prompt/now', { method: 'POST' })
      if (!res.ok) {
        const txt = await res.text()
        throw new Error(txt || 'Failed to send prompt')
      }
      setInfo('Prompt sent (admin).')
    } catch (e) {
      setAuthError(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card">
      <h1>Momentary</h1>

      {authError ? <p style={{ color: 'crimson' }}>{authError}</p> : null}
      {info ? <p style={{ color: '#2e7d32' }}>{info}</p> : null}

      {me ? (
        <>
          <p>{statusText}</p>
          <div style={{ display: 'flex', gap: 12, justifyContent: 'center' }}>
            <button type="button" disabled={busy} onClick={doLogout}>
              Log out
            </button>
            <button type="button" disabled={busy || !pushSupported} onClick={enablePush}>
              Enable push
            </button>
            <button type="button" disabled={busy} onClick={sendTestPrompt}>
              Send test prompt
            </button>
          </div>

          <div style={{ marginTop: 16 }}>
            <h2 style={{ fontSize: 16, marginBottom: 8 }}>Prompt</h2>
            {prompt ? (
              prompt.active ? (
                <>
                  <p>Active — {prompt.secondsRemaining}s left</p>
                  <PhotoUpload apiFetch={apiFetch} disabled={busy} />
                </>
              ) : (
                <p>Not active right now.</p>
              )
            ) : (
              <p>Loading…</p>
            )}
          </div>
        </>
      ) : (
        <>
          <p>Not logged in.</p>
          <div style={{ display: 'grid', gap: 12, maxWidth: 420, margin: '0 auto' }}>
            <div>
              <h2 style={{ fontSize: 16, marginBottom: 6 }}>Login</h2>
              <input
                placeholder="username"
                value={loginUsername}
                onChange={(e) => setLoginUsername(e.target.value)}
              />
              <input
                placeholder="password"
                type="password"
                value={loginPassword}
                onChange={(e) => setLoginPassword(e.target.value)}
              />
              <button type="button" disabled={busy} onClick={doLogin}>
                Login
              </button>
            </div>

            <div>
              <h2 style={{ fontSize: 16, marginBottom: 6 }}>Register</h2>
              <input
                placeholder="username"
                value={regUsername}
                onChange={(e) => setRegUsername(e.target.value)}
              />
              <input
                placeholder="password (min 8 chars)"
                type="password"
                value={regPassword}
                onChange={(e) => setRegPassword(e.target.value)}
              />
              <button type="button" disabled={busy} onClick={doRegister}>
                Register
              </button>
            </div>
          </div>

          <p className="read-the-docs">
            API base: <code>{apiBase || '(same origin)'}</code>
          </p>
        </>
      )}
    </div>
  )
}

export default App

function PhotoUpload({
  apiFetch,
  disabled,
}: {
  apiFetch: (path: string, init?: RequestInit) => Promise<Response>
  disabled: boolean
}) {
  const [file, setFile] = useState<File | null>(null)
  const [msg, setMsg] = useState<string | null>(null)

  async function submit() {
    setMsg(null)
    if (!file) return
    const form = new FormData()
    form.append('file', file)
    const res = await apiFetch('/photos/upload', { method: 'POST', body: form })
    if (!res.ok) {
      const txt = await res.text()
      setMsg(txt || 'Upload failed')
      return
    }
    setFile(null)
    setMsg('Uploaded.')
  }

  return (
    <div style={{ display: 'grid', gap: 8, justifyItems: 'center' }}>
      <input
        type="file"
        accept="image/*"
        capture="environment"
        disabled={disabled}
        onChange={(e) => setFile(e.target.files?.[0] ?? null)}
      />
      <button type="button" disabled={disabled || !file} onClick={submit}>
        Upload photo
      </button>
      {msg ? <p className="read-the-docs">{msg}</p> : null}
    </div>
  )
}
