import React, { useEffect, useState } from 'react'
import api from '../api'

interface GeneratedFile {
  file_id: string
  user_id: string
  file_type: string
  file_name: string
  file_size_bytes: number | null
  storage_path: string
  created_at: string
}

function fmtDate(iso: string) {
  return new Date(iso).toLocaleString('en-PK', { hour12: true, dateStyle: 'short', timeStyle: 'short' })
}

function fmtSize(bytes: number | null) {
  if (!bytes) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
}

const GeneratedFilesPage: React.FC = () => {
  const [files, setFiles] = useState<GeneratedFile[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [deleting, setDeleting] = useState<string | null>(null)

  const fetchFiles = async () => {
    setLoading(true)
    setError('')
    try {
      const res = await api.get('/files', { params: { limit: 100 } })
      setFiles(res.data)
    } catch {
      setError('Failed to load files')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { fetchFiles() }, [])

  const handleDelete = async (fileId: string, fileName: string) => {
    if (!confirm(`Delete "${fileName}"? This cannot be undone.`)) return
    setDeleting(fileId)
    try {
      await api.delete(`/files/${fileId}`)
      setFiles(f => f.filter(x => x.file_id !== fileId))
    } catch {
      alert('Failed to delete file.')
    } finally {
      setDeleting(null)
    }
  }

  const total = files.reduce((sum, f) => sum + (f.file_size_bytes || 0), 0)

  return (
    <div className="main-content">
      <div className="page-header">
        <div>
          <div className="page-title">Generated Files</div>
          <div className="page-subtitle">
            {files.length} file{files.length !== 1 ? 's' : ''} · {fmtSize(total)} total storage
          </div>
        </div>
        <button className="btn btn-primary" onClick={fetchFiles} style={{ fontSize: 12 }}>
          ↻ Refresh
        </button>
      </div>

      <div className="page-body">
        <div className="table-wrapper">
          {loading && <div className="loading-state"><div className="spinner"/><span>Loading…</span></div>}
          {error && <div className="loading-state" style={{ color: 'var(--error)' }}>{error}</div>}

          {!loading && !error && (
            files.length === 0 ? (
              <div className="empty-state">
                <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5} style={{ color: 'var(--text-muted)' }}>
                  <path d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z"/>
                </svg>
                <span>No generated files yet.</span>
              </div>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>File Name</th>
                    <th>Type</th>
                    <th>Size</th>
                    <th>User ID</th>
                    <th>Created</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {files.map(f => (
                    <tr key={f.file_id}>
                      <td>
                        <a
                          href={`/api/files/${f.file_id}/download`}
                          style={{ color: 'var(--gold)', fontWeight: 600 }}
                          target="_blank"
                          rel="noreferrer"
                          onClick={e => e.stopPropagation()}
                        >
                          ↓ {f.file_name}
                        </a>
                      </td>
                      <td>
                        <span className={`badge badge-${f.file_type}`}>
                          {f.file_type?.toUpperCase()}
                        </span>
                      </td>
                      <td>{fmtSize(f.file_size_bytes)}</td>
                      <td className="td-mono">{f.user_id?.slice(0, 8)}…</td>
                      <td style={{ color: 'var(--text-muted)', fontSize: 12 }}>
                        {f.created_at ? fmtDate(f.created_at) : '—'}
                      </td>
                      <td>
                        <button
                          className="btn btn-danger"
                          style={{ fontSize: 11, padding: '4px 10px' }}
                          onClick={() => handleDelete(f.file_id, f.file_name)}
                          disabled={deleting === f.file_id}
                        >
                          {deleting === f.file_id ? '…' : '✕ Delete'}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          )}
        </div>
      </div>
    </div>
  )
}

export default GeneratedFilesPage
