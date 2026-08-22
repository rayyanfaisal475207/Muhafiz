import React from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { AuthProvider, useAuth, type AdminRole } from './AuthContext'
import LoginPage from './pages/LoginPage'
import Sidebar from './components/Sidebar'
import DashboardPage from './pages/DashboardPage'
import ErrorsPage from './pages/ErrorsPage'
import KnowledgeBasePage from './pages/KnowledgeBasePage'
import ReviewQueuePage from './pages/ReviewQueuePage'
import RunHistoryPage from './pages/RunHistoryPage'
import GeneratedFilesPage from './pages/GeneratedFilesPage'
import UsersPage from './pages/UsersPage'
import McpCallLogPage from './pages/McpCallLogPage'
import ProfilePage from './pages/ProfilePage'
import EntityEvalPage from './pages/EntityEvalPage'
import AuditLogPage from './pages/AuditLogPage'
import CaseManagementPage from './pages/CaseManagementPage'
import IngestionQualityPage from './pages/IngestionQualityPage'
import './index.css'

// Where each role lands if it hits a route above its own tier (including "/"
// itself — the Dashboard is platform-admin-only on the backend, so a
// supervisor/station-admin account can't just default-land there).
const ROLE_HOME: Record<AdminRole, string> = {
  'platform-admin': '/',
  'station-admin': '/cases',
  'supervisor': '/review-queue',
}

const RequireRole: React.FC<{ min: AdminRole[]; children: React.ReactNode }> = ({ min, children }) => {
  const { role } = useAuth()
  if (!role) return <Navigate to="/login" replace />
  if (!min.includes(role)) return <Navigate to={ROLE_HOME[role]} replace />
  return <>{children}</>
}

const PLATFORM_ADMIN: AdminRole[] = ['platform-admin']
const SUPERVISOR_PLUS: AdminRole[] = ['supervisor', 'station-admin', 'platform-admin']

const ProtectedLayout: React.FC = () => {
  const { isAuthenticated } = useAuth()
  if (!isAuthenticated) return <Navigate to="/login" replace />
  return (
    <div className="admin-shell">
      <Sidebar />
      <Routes>
        <Route path="/" element={<RequireRole min={PLATFORM_ADMIN}><DashboardPage /></RequireRole>} />
        <Route path="/knowledge-base" element={<RequireRole min={PLATFORM_ADMIN}><KnowledgeBasePage /></RequireRole>} />
        <Route path="/errors" element={<RequireRole min={PLATFORM_ADMIN}><ErrorsPage /></RequireRole>} />
        <Route path="/runs" element={<RequireRole min={PLATFORM_ADMIN}><RunHistoryPage /></RequireRole>} />
        <Route path="/files" element={<RequireRole min={PLATFORM_ADMIN}><GeneratedFilesPage /></RequireRole>} />
        <Route path="/mcp" element={<RequireRole min={PLATFORM_ADMIN}><McpCallLogPage /></RequireRole>} />
        <Route path="/users" element={<RequireRole min={PLATFORM_ADMIN}><UsersPage /></RequireRole>} />
        <Route path="/review-queue" element={<RequireRole min={SUPERVISOR_PLUS}><ReviewQueuePage /></RequireRole>} />
        <Route path="/ingestion-quality" element={<RequireRole min={SUPERVISOR_PLUS}><IngestionQualityPage /></RequireRole>} />
        <Route path="/eval/entity-resolution" element={<RequireRole min={SUPERVISOR_PLUS}><EntityEvalPage /></RequireRole>} />
        <Route path="/audit-logs" element={<RequireRole min={PLATFORM_ADMIN}><AuditLogPage /></RequireRole>} />
        <Route path="/cases" element={<RequireRole min={SUPERVISOR_PLUS}><CaseManagementPage /></RequireRole>} />
        <Route path="/profile" element={<ProfilePage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </div>
  )
}

const App: React.FC = () => {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<LoginRouteGuard />} />
          <Route path="/*" element={<ProtectedLayout />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  )
}

// Redirect to dashboard if already logged in
const LoginRouteGuard: React.FC = () => {
  const { isAuthenticated } = useAuth()
  if (isAuthenticated) return <Navigate to="/" replace />
  return <LoginPage />
}

export default App
