import { getToken, clearToken } from './auth'

// In prod behind Caddy: /lk/api → Caddy strips /lk → nginx sees /api → proxies to API
// In dev (localhost:3003): /api → nginx proxies directly to API
const BASE_URL = `${import.meta.env.BASE_URL}api`

class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const token = getToken()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...init?.headers as Record<string, string>,
  }

  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }

  const response = await fetch(url, { ...init, headers })

  if (response.status === 401) {
    clearToken()
    window.location.href = `${import.meta.env.BASE_URL}auth`
    throw new ApiError(401, 'Unauthorized')
  }

  if (!response.ok) {
    throw new ApiError(response.status, `${response.status} ${response.statusText}`)
  }

  return response.json()
}

export const api = {
  get: <T>(path: string) => request<T>(`${BASE_URL}${path}`),
  post: <T>(path: string, body: unknown) =>
    request<T>(`${BASE_URL}${path}`, { method: 'POST', body: JSON.stringify(body) }),
}
