const BASE_URL = '/api'

class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...init?.headers,
    },
  })

  if (!response.ok) {
    throw new ApiError(response.status, `${response.status} ${response.statusText}`)
  }

  return response.json()
}

export const api = {
  get: <T>(path: string) => request<T>(`${BASE_URL}${path}`),
  post: <T>(path: string, body: unknown) =>
    request<T>(`${BASE_URL}${path}`, { method: 'POST', body: JSON.stringify(body) }),
  patch: <T>(path: string, body: unknown) =>
    request<T>(`${BASE_URL}${path}`, { method: 'PATCH', body: JSON.stringify(body) }),
  delete: <T>(path: string) => request<T>(`${BASE_URL}${path}`, { method: 'DELETE' }),
  /** Fetch a path without the /api prefix (e.g. /debug/queues) */
  raw: <T>(path: string) => request<T>(path),
}
