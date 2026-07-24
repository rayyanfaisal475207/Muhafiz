import React from 'react';
import { useAuth } from '../AuthContext';
import { useNavigate } from 'react-router-dom';

const ROLE_LABELS: Record<string, string> = {
  'investigator': 'Investigator',
  'supervisor': 'Supervisor',
  'station-admin': 'Station Admin',
  'platform-admin': 'Platform Admin',
};

const ProfilePage: React.FC = () => {
  const { logout, role, email } = useAuth();
  const navigate = useNavigate();

  const handleLogout = () => {
    logout();
    navigate('/login');
  };

  return (
    <div className="page-body">
      <div style={{ maxWidth: 600, margin: '0 auto', marginTop: 40 }}>
        <h2 style={{ marginBottom: 24, color: 'var(--text-primary)' }}>Admin Profile & Settings</h2>

        <div className="card" style={{ padding: 24, marginBottom: 24 }}>
          <h3 style={{ fontSize: 16, marginBottom: 16, color: 'var(--text-primary)' }}>Account Information</h3>
          <div style={{ marginBottom: 12 }}>
            <span style={{ color: 'var(--text-muted)', display: 'inline-block', width: 100 }}>Email:</span>
            <span style={{ fontWeight: 500, color: 'var(--text-primary)' }}>{email || '—'}</span>
          </div>
          <div style={{ marginBottom: 12 }}>
            <span style={{ color: 'var(--text-muted)', display: 'inline-block', width: 100 }}>Role:</span>
            <span style={{ fontWeight: 500, color: 'var(--text-primary)' }}>{role ? (ROLE_LABELS[role] || role) : '—'}</span>
          </div>
          <div style={{ marginBottom: 24 }}>
            <span style={{ color: 'var(--text-muted)', display: 'inline-block', width: 100 }}>Status:</span>
            <span className="badge badge-success">Active</span>
          </div>

          <p style={{ color: 'var(--text-secondary)', fontSize: 13, lineHeight: 1.5 }}>
            Administrative settings are currently managed via environment variables and direct database access.
            If you need to change your admin password, update the <code>users</code> table in Postgres directly.
          </p>
        </div>

        <button 
          onClick={handleLogout}
          style={{
            padding: '8px 16px',
            background: 'var(--error-bg)',
            color: 'var(--error)',
            border: '1px solid var(--error-border)',
            borderRadius: 6,
            cursor: 'pointer',
            fontWeight: 500
          }}
        >
          Sign Out of Admin Panel
        </button>
      </div>
    </div>
  );
};

export default ProfilePage;
