import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router'
import { setToken, isAuthenticated } from '@/lib/auth'
import type { TokenExchangeResponse } from '@/types/api'
import Spinner from '@/components/Spinner'
import ErrorMessage from '@/components/ErrorMessage'

export default function AuthPage() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const [exchangeError, setExchangeError] = useState<string | null>(null)

  // A missing token is visible from the URL alone, so it is derived rather than
  // written into state from inside the effect.
  const token = searchParams.get('token')
  const error = token ? exchangeError : 'Ссылка недействительна. Запросите новую через Telegram.'

  useEffect(() => {
    if (isAuthenticated()) {
      navigate('/projects', { replace: true })
      return
    }

    if (!token) return

    fetch(`${import.meta.env.BASE_URL}api/lk/auth/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
    })
      .then(async (res) => {
        if (!res.ok) {
          const detail = await res.json().catch(() => null)
          throw new Error(detail?.detail ?? 'Токен недействителен или истёк')
        }
        return res.json() as Promise<TokenExchangeResponse>
      })
      .then((data) => {
        setToken(data.access_token)
        navigate('/projects', { replace: true })
      })
      .catch((err: Error) => {
        setExchangeError(err.message)
      })
  }, [token, navigate])

  return (
    <div className="flex min-h-svh items-center justify-center bg-background px-4">
      <div className="w-full max-w-sm text-center">
        {error ? (
          <ErrorMessage message={error} />
        ) : (
          <div className="flex flex-col items-center gap-3">
            <Spinner className="h-8 w-8" />
            <p className="text-sm text-muted-foreground">Входим...</p>
          </div>
        )}
      </div>
    </div>
  )
}
