import axios from 'axios'

// Separate from `api.ts` (baseURL `/api/admin`) because Case CRUD and
// case_assignments live under `/api/cases`, not `/api/admin/*` — a different
// router, same backend. Same CSRF pattern as `api.ts`/`AuthContext.tsx`.
const casesApi = axios.create({
  baseURL: '/api',
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json',
  },
})

casesApi.interceptors.request.use((config) => {
  const method = config.method?.toLowerCase()
  if (method && ['post', 'put', 'delete', 'patch'].includes(method)) {
    const match = document.cookie.match(new RegExp('(^| )csrf_token=([^;]+)'))
    if (match) {
      config.headers['X-CSRF-Token'] = match[2]
    }
  }
  return config
})

export default casesApi
