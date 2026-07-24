import React, { useEffect, useState, useCallback } from 'react'
import casesApi from '../casesApi'
import { useAuth } from '../AuthContext'
import { Card, formatWhen } from '../components/common'

interface CaseRecord {
  case_id: string
  fir_number?: string | null
  crime_category?: string | null
  investigation_officer?: string | null
  police_station?: string | null
  incident_date?: string | null
  investigation_status?: string | null
  location?: string | null
  description?: string | null
  created_at: string
  updated_at: string
}

interface Assignment {
  user_id: string
  email?: string | null
  role: string
}

const ASSIGNMENT_ROLES = ['investigator', 'supervisor', 'station-admin', 'platform-admin']

// station-admin/platform-admin can manage assignments; a plain supervisor
// gets a read-only view of the same page (matches the backend's own
// require_role("station-admin") gate on the assignment endpoints).
const CAN_MANAGE_ASSIGNMENTS = ['station-admin', 'platform-admin']

const CaseManagementPage: React.FC = () => {
  const { role } = useAuth()
  const canManage = !!role && CAN_MANAGE_ASSIGNMENTS.includes(role)

  const [cases, setCases] = useState<CaseRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<CaseRecord | null>(null)

  const [assignments, setAssignments] = useState<Assignment[]>([])
  const [assignmentsLoading, setAssignmentsLoading] = useState(false)
  const [assignError, setAssignError] = useState('')

  const [newEmail, setNewEmail] = useState('')
  const [newRole, setNewRole] = useState('investigator')
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    const fetchCases = async () => {
      setLoading(true)
      setError('')
      try {
        const res = await casesApi.get<CaseRecord[]>('/cases/')
        setCases(res.data)
      } catch {
        setError('Failed to load cases')
      } finally {
        setLoading(false)
      }
    }
    fetchCases()
  }, [])

  const loadAssignments = useCallback(async (caseId: string) => {
    setAssignmentsLoading(true)
    setAssignError('')
    try {
      const res = await casesApi.get<Assignment[]>(`/cases/${caseId}/assignments/`)
      setAssignments(res.data)
    } catch (err: any) {
      // A plain supervisor can view the case but not its assignments
      // (backend requires station-admin+ for this endpoint) — show that
      // plainly rather than a raw request failure.
      if (err?.response?.status === 403) {
        setAssignError('Your role can view this case but not its assignment list.')
      } else {
        setAssignError('Failed to load assignments')
      }
      setAssignments([])
    } finally {
      setAssignmentsLoading(false)
    }
  }, [])

  const handleSelect = (c: CaseRecord) => {
    setSelected(c)
    setNewEmail('')
    setNewRole('investigator')
    loadAssignments(c.case_id)
  }

  const handleAssign = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!selected || !newEmail.trim()) return
    setSubmitting(true)
    setAssignError('')
    try {
      await casesApi.post(`/cases/${selected.case_id}/assignments/`, {
        email: newEmail.trim(),
        role: newRole,
      })
      setNewEmail('')
      await loadAssignments(selected.case_id)
    } catch (err: any) {
      setAssignError(err?.response?.data?.detail || 'Failed to assign user')
    } finally {
      setSubmitting(false)
    }
  }

  const handleUnassign = async (userId: string) => {
    if (!selected) return
    setAssignError('')
    try {
      await casesApi.delete(`/cases/${selected.case_id}/assignments/${userId}`)
      await loadAssignments(selected.case_id)
    } catch {
      setAssignError('Failed to remove assignment')
    }
  }

  return (
    <div className="main-content">
      <div className="page-header">
        <div>
          <div className="page-title">Case Management</div>
          <div className="page-subtitle">
            {cases.length} case{cases.length !== 1 ? 's' : ''}
            {role === 'platform-admin' ? ' (all cases)' : ' (cases you are assigned to)'}
          </div>
        </div>
      </div>

      <div className="page-body" style={{ display: 'grid', gridTemplateColumns: '1.3fr 1fr', gap: 20, alignItems: 'start' }}>
        <Card title="Cases" sub={canManage ? 'Select a case to manage its assignments' : 'Select a case to view its assignments'}>
          {loading && <div className="loading-state"><div className="spinner" /><span>Loading…</span></div>}
          {error && <div className="loading-state" style={{ color: 'var(--error)' }}>{error}</div>}
          {!loading && !error && (
            cases.length === 0 ? (
              <div className="empty-state">No cases visible to your account.</div>
            ) : (
              <div className="table-wrapper">
                <table>
                  <thead>
                    <tr>
                      <th>FIR #</th>
                      <th>Category</th>
                      <th>Station</th>
                      <th>Status</th>
                      <th>IO</th>
                    </tr>
                  </thead>
                  <tbody>
                    {cases.map((c) => (
                      <tr
                        key={c.case_id}
                        onClick={() => handleSelect(c)}
                        style={{
                          cursor: 'pointer',
                          background: selected?.case_id === c.case_id ? 'var(--accent-soft)' : undefined,
                        }}
                      >
                        <td style={{ fontWeight: 500, color: 'var(--text-primary)' }}>{c.fir_number || c.case_id}</td>
                        <td>{c.crime_category || <span style={{ color: 'var(--text-muted)' }}>—</span>}</td>
                        <td>{c.police_station || <span style={{ color: 'var(--text-muted)' }}>—</span>}</td>
                        <td>
                          <span className="badge badge-accent">{c.investigation_status || 'Unknown'}</span>
                        </td>
                        <td style={{ color: 'var(--text-muted)', fontSize: 12 }}>{c.investigation_officer || '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          )}
        </Card>

        <Card title={selected ? (selected.fir_number || selected.case_id) : 'Case detail'} sub={selected ? selected.case_id : 'Pick a case from the list'}>
          {!selected && <div className="empty-state">No case selected.</div>}
          {selected && (
            <>
              <div style={{ marginBottom: 16, fontSize: 13, color: 'var(--text-secondary)', display: 'flex', flexDirection: 'column', gap: 6 }}>
                <div><span style={{ color: 'var(--text-muted)' }}>Category:</span> {selected.crime_category || '—'}</div>
                <div><span style={{ color: 'var(--text-muted)' }}>Station:</span> {selected.police_station || '—'}</div>
                <div><span style={{ color: 'var(--text-muted)' }}>Status:</span> {selected.investigation_status || '—'}</div>
                <div><span style={{ color: 'var(--text-muted)' }}>Investigating Officer:</span> {selected.investigation_officer || '—'}</div>
                <div><span style={{ color: 'var(--text-muted)' }}>Updated:</span> {formatWhen(selected.updated_at)}</div>
                {selected.description && (
                  <div style={{ marginTop: 4 }}><span style={{ color: 'var(--text-muted)' }}>Description:</span> {selected.description}</div>
                )}
              </div>

              <div className="card-title" style={{ marginBottom: 8 }}>Assigned users</div>
              {assignmentsLoading && <div className="loading-state"><div className="spinner" /><span>Loading…</span></div>}
              {assignError && <div style={{ color: 'var(--error)', fontSize: 13, marginBottom: 8 }}>{assignError}</div>}
              {!assignmentsLoading && assignments.length === 0 && !assignError && (
                <div className="empty-state" style={{ marginBottom: 12 }}>No one is assigned to this case yet.</div>
              )}
              {!assignmentsLoading && assignments.length > 0 && (
                <ul style={{ listStyle: 'none', padding: 0, margin: '0 0 16px 0', display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {assignments.map((a) => (
                    <li
                      key={a.user_id}
                      style={{
                        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                        padding: '6px 10px', borderRadius: 6, background: 'var(--bg-surface-2)',
                      }}
                    >
                      <span style={{ fontSize: 13 }}>{a.email || a.user_id}</span>
                      <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <span className="badge badge-accent">{a.role}</span>
                        {canManage && (
                          <button
                            onClick={() => handleUnassign(a.user_id)}
                            className="btn-ghost"
                            style={{ fontSize: 12, padding: '2px 8px' }}
                          >
                            Remove
                          </button>
                        )}
                      </span>
                    </li>
                  ))}
                </ul>
              )}

              {canManage && (
                <form onSubmit={handleAssign} style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
                  <div style={{ flex: 1 }}>
                    <label style={{ display: 'block', fontSize: 12, color: 'var(--text-muted)', marginBottom: 4 }}>
                      Assign by email
                    </label>
                    <input
                      type="email"
                      required
                      value={newEmail}
                      onChange={(e) => setNewEmail(e.target.value)}
                      placeholder="investigator@example.com"
                      className="text-input"
                      style={{ width: '100%' }}
                    />
                  </div>
                  <div>
                    <label style={{ display: 'block', fontSize: 12, color: 'var(--text-muted)', marginBottom: 4 }}>
                      Role
                    </label>
                    <select value={newRole} onChange={(e) => setNewRole(e.target.value)} className="text-input">
                      {ASSIGNMENT_ROLES.map((r) => (
                        <option key={r} value={r}>{r}</option>
                      ))}
                    </select>
                  </div>
                  <button type="submit" disabled={submitting || !newEmail.trim()} className="btn-accent" style={{ padding: '7px 14px' }}>
                    {submitting ? 'Assigning…' : 'Assign'}
                  </button>
                </form>
              )}
            </>
          )}
        </Card>
      </div>
    </div>
  )
}

export default CaseManagementPage
