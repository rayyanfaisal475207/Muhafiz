import axios from 'axios'

const api = axios.create({
  baseURL: '/api/admin',
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json',
  },
})

// The backend enforces Double-Submit CSRF on all mutating requests
// (see src/auth/jwt.py). Without this header every DELETE/POST from the
// admin panel was rejected with 403.
api.interceptors.request.use((config) => {
  const method = config.method?.toLowerCase()
  if (method && ['post', 'put', 'delete', 'patch'].includes(method)) {
    const match = document.cookie.match(new RegExp('(^| )csrf_token=([^;]+)'))
    if (match) {
      config.headers['X-CSRF-Token'] = match[2]
    }
  }
  return config
})

export default api
