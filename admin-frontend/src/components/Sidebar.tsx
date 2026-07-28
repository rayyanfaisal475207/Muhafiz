import React from 'react'
import { NavLink, useNavigate } from 'react-router-dom'
import { useAuth, type AdminRole } from '../AuthContext'

const icon = (path: React.ReactNode) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7}
       strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {path}
  </svg>
)

const PLATFORM_ADMIN: AdminRole[] = ['platform-admin']
const SUPERVISOR_PLUS: AdminRole[] = ['supervisor', 'station-admin', 'platform-admin']

// Grouped by the question each page answers, rather than by which table it reads.
// `minRole` mirrors the backend's own require_role(...) gate for that page —
// an item is hidden entirely for a role that would just get redirected/403'd.
const navGroups: Array<{ label: string; items: Array<{ to: string; label: string; icon: React.ReactNode; minRole: AdminRole[] }> }> = [
  {
    label: 'Overview',
    items: [
      {
        to: '/',
        label: 'Dashboard',
        icon: icon(<><path d="M4 13.5h5v6H4zM4 4.5h5v6H4zM15 10.5h5v9h-5zM15 4.5h5v3h-5z" /></>),
        minRole: PLATFORM_ADMIN,
      },
    ],
  },
  {
    label: 'Knowledge',
    items: [
      {
        to: '/knowledge-base',
        label: 'Knowledge Base',
        icon: icon(<><path d="M5 4.5h9l5 5v10a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-14a1 1 0 0 1 1-1Z" /><path d="M14 4.5v5h5" /><path d="M8 13h7M8 16.5h4" /></>),
        minRole: PLATFORM_ADMIN,
      },
      {
        to: '/cases',
        label: 'Case Management',
        icon: icon(<><path d="M4 6.5h9l2 2h5v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-10a1 1 0 0 1 1-1Z" /></>),
        minRole: SUPERVISOR_PLUS,
      },
      {
        to: '/review-queue',
        label: 'Review Queue',
        icon: icon(<><circle cx="12" cy="12" r="8.5" /><path d="M8.5 12.5l2.3 2.3L15.8 9.7" /></>),
        minRole: SUPERVISOR_PLUS,
      },
      {
        to: '/eval/entity-resolution',
        label: 'Entity Eval',
        icon: icon(<><path d="M3 3v18h18" /><path d="M18.7 8l-5.1 5.2-2.8-2.7L7 14.3" /></>),
        minRole: SUPERVISOR_PLUS,
      },
    ],
  },
  {
    label: 'Monitoring',
    items: [
      {
        to: '/audit-logs',
        label: 'Audit Logs',
        icon: icon(<><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /></>),
        minRole: PLATFORM_ADMIN,
      },
      {
        to: '/errors',
        label: 'Errors',
        icon: icon(<><circle cx="12" cy="12" r="8.5" /><path d="M12 7.8v5M12 15.8h.01" /></>),
        minRole: PLATFORM_ADMIN,
      },
      {
        to: '/runs',
        label: 'Run History',
        icon: icon(<><path d="M4 6.5h16M4 12h16M4 17.5h10" /></>),
        minRole: PLATFORM_ADMIN,
      },
      {
        to: '/mcp',
        label: 'MCP Calls',
        icon: icon(<><path d="M10 20l4-16M18 8l4 4-4 4M6 16l-4-4 4-4" /></>),
        minRole: PLATFORM_ADMIN,
      },
    ],
  },
  {
    label: 'Accounts',
    items: [
      {
        to: '/files',
        label: 'Generated Files',
        icon: icon(<><path d="M7 20h10a2 2 0 0 0 2-2V9.4a1 1 0 0 0-.3-.7l-5.4-5.4a1 1 0 0 0-.7-.3H7a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z" /></>),
        minRole: PLATFORM_ADMIN,
      },
      {
        to: '/users',
        label: 'Users',
        icon: icon(<><circle cx="9" cy="8" r="3.2" /><path d="M3.5 19.5a5.5 5.5 0 0 1 11 0" /><path d="M16 5.6a3.2 3.2 0 0 1 0 6.3M17.5 14.4a5.5 5.5 0 0 1 3 5.1" /></>),
        minRole: PLATFORM_ADMIN,
      },
      {
        to: '/profile',
        label: 'Settings',
        icon: icon(<><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1.08-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 8.6a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z" /></>),
        minRole: SUPERVISOR_PLUS,
      },
    ],
  },
]

/** The Muhafiz spark — same mark as the product. */
const LogoMark = () => (
  <svg viewBox="0 0 64 64" width="26" height="26" aria-hidden="true">
    <path d="M0 0h50a14 14 0 0 1 14 14v36a14 14 0 0 1-14 14H14A14 14 0 0 1 0 50V0Z" fill="var(--accent)" />
    <path
      d="M32 12C33.1 21.4 42.6 30.9 52 32C42.6 33.1 33.1 42.6 32 52C30.9 42.6 21.4 33.1 12 32C21.4 30.9 30.9 21.4 32 12Z"
      fill="var(--bg-base)"
    />
  </svg>
)

const Sidebar: React.FC = () => {
  const { logout, role } = useAuth()
  const navigate = useNavigate()

  const handleLogout = () => {
    logout()
    navigate('/login')
  }

  // Hide items the current role would just be redirected away from — see
  // App.tsx's RequireRole, which enforces this at the route level too.
  const visibleGroups = navGroups
    .map((group) => ({
      ...group,
      items: group.items.filter((item) => !role || item.minRole.includes(role)),
    }))
    .filter((group) => group.items.length > 0)

  return (
    <aside className="sidebar">
      <div className="sidebar-brand">
        <LogoMark />
        <span className="sidebar-brand-text">Muhafiz</span>
        <span className="sidebar-badge">Admin</span>
      </div>

      <nav style={{ overflowY: 'auto' }}>
        {visibleGroups.map((group) => (
          <div key={group.label}>
            <div className="nav-section-label">{group.label}</div>
            {group.items.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === '/'}
                className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
              >
                {item.icon}
                {item.label}
              </NavLink>
            ))}
          </div>
        ))}
      </nav>

      <div className="sidebar-footer">
        <button onClick={handleLogout} className="nav-link" style={{ width: '100%', border: 'none', background: 'none', cursor: 'pointer', font: 'inherit' }}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M15 16l4-4m0 0l-4-4m4 4H8M12 20H7a3 3 0 0 1-3-3V7a3 3 0 0 1 3-3h5" />
          </svg>
          Sign out
        </button>
      </div>
    </aside>
  )
}

export default Sidebar
