import './App.css'
import { useEffect, useMemo, useRef, useState } from 'react'

type TelegramUser = {
  id: number
  username?: string
  first_name?: string
  last_name?: string
  photo_url?: string
  auth_date?: number
  hash?: string
}

declare global {
  interface Window {
    onTelegramAuth?: (user: TelegramUser) => void
  }
}

const STORAGE_KEY = 'momentary.telegramUser'

function loadStoredUser(): TelegramUser | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    return JSON.parse(raw) as TelegramUser
  } catch {
    return null
  }
}

function storeUser(user: TelegramUser | null) {
  if (!user) {
    localStorage.removeItem(STORAGE_KEY)
    return
  }
  localStorage.setItem(STORAGE_KEY, JSON.stringify(user))
}

function App() {
  const botUsername = (import.meta.env.VITE_TELEGRAM_BOT_USERNAME as string | undefined)
    ?.trim()
    ?.replace(/^@/, '')
  const [user, setUser] = useState<TelegramUser | null>(() => loadStoredUser())
  const loginContainerRef = useRef<HTMLDivElement | null>(null)

  const display = useMemo(() => {
    if (!user) return null
    const handle = user.username ? `@${user.username}` : '—'
    return `Logged in as ${handle} (ID: ${user.id})`
  }, [user])

  useEffect(() => {
    window.onTelegramAuth = (incoming: TelegramUser) => {
      setUser(incoming)
      storeUser(incoming)
    }
    return () => {
      delete window.onTelegramAuth
    }
  }, [])

  useEffect(() => {
    const container = loginContainerRef.current
    if (!container) return
    container.innerHTML = ''
    if (!botUsername) return
    if (user) return

    const script = document.createElement('script')
    script.async = true
    script.src = 'https://telegram.org/js/telegram-widget.js?22'
    script.setAttribute('data-telegram-login', botUsername)
    script.setAttribute('data-size', 'large')
    script.setAttribute('data-onauth', 'onTelegramAuth(user)')
    script.setAttribute('data-request-access', 'write')
    container.appendChild(script)
  }, [botUsername, user])

  return (
    <div className="card">
      <h1>Momentary</h1>

      {!botUsername ? (
        <p className="read-the-docs">
          Missing <code>VITE_TELEGRAM_BOT_USERNAME</code>.
        </p>
      ) : user ? (
        <>
          <p>{display}</p>
          <button
            type="button"
            onClick={() => {
              setUser(null)
              storeUser(null)
            }}
          >
            Log out
          </button>
        </>
      ) : (
        <>
          <p>Not logged in.</p>
          <div ref={loginContainerRef} />
          <p className="read-the-docs">
            This uses the Telegram widget callback to show a logged-in state.
          </p>
        </>
      )}
    </div>
  )
}

export default App
