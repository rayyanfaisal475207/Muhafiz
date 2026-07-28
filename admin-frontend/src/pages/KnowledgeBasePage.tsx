// ============================================================
// Knowledge base — upload documents into the SHARED corpus.
//
// This is the real ingestion path, and it is admin-only. A file uploaded here
// is chunked, embedded, and written into the same `document_chunks` table the
// retriever reads — the same set of documents, not a separate store.
//
// Users cannot reach this. From the chat composer they attach files to a single
// conversation; those are never embedded and never enter this corpus.
// ============================================================

import React, { useCallback, useEffect, useRef, useState } from 'react'
import api from '../api'
import { BreakdownChart, NoData, formatNumber } from '../components/charts'
import {
  Card, StatCard, InstrumentationBanner, StatusBadge, formatBytes, formatWhen,
} from '../components/common'

interface KbDoc {
  doc_id: string
  filename: string
  doc_type: string | null
  chunk_count: number
  is_global: boolean
  ingested_at: string | null
}

interface KbStats {
  total_chunks: number
  total_documents: number
  documents: KbDoc[]
}

interface Job {
  job_id: string
  filename: string
  file_type: string | null
  file_size_bytes: number | null
  status: 'processing' | 'success' | 'failed'
  chunks_added: number | null
  error_message: string | null
  duration_ms: number | null
  started_at: string | null
}

const ACCEPTED = '.pdf,.txt,.md,.csv,.xlsx,.xls,.html,.htm,.docx,.png,.jpg,.jpeg,.webp'

const KnowledgeBasePage: React.FC = () => {
  const [stats, setStats] = useState<KbStats | null>(null)
  const [jobs, setJobs] = useState<Job[]>([])
  const [missing, setMissing] = useState<string[]>([])
  const [dragging, setDragging] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [deleting, setDeleting] = useState<string | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)

  const refresh = useCallback(async () => {
    try {
      const [s, j, inst] = await Promise.all([
        api.get<KbStats>('/kb/stats'),
        api.get<Job[]>('/kb/jobs'),
        api.get<{ tables: Record<string, boolean> }>('/instrumentation'),
      ])
      setStats(s.data)
      setJobs(j.data)
      setMissing(
        Object.entries(inst.data.tables)
          .filter(([, present]) => !present)
          .map(([name]) => name),
      )
      setLoadError(null)
      return j.data
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        'Failed to load the knowledge base'
      setLoadError(detail)
      return []
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  // While anything is still chunking, poll: ingestion is a background task and
  // the row should flip to success/failed on its own.
  useEffect(() => {
    if (!jobs.some((j) => j.status === 'processing')) return
    const id = window.setInterval(() => { refresh() }, 3000)
    return () => window.clearInterval(id)
  }, [jobs, refresh])

  const upload = useCallback(async (files: FileList | null) => {
    if (!files?.length) return
    setUploadError(null)

    for (const file of Array.from(files)) {
      const form = new FormData()
      form.append('file', file)
      try {
        await api.post('/kb/upload', form)
      } catch (err: unknown) {
        const detail =
          (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
          `Could not upload ${file.name}`
        setUploadError(detail)
      }
    }
    refresh()
  }, [refresh])

  const remove = useCallback(async (filename: string) => {
    if (!window.confirm(`Delete "${filename}" and all of its chunks from the knowledge base?`)) return
    setDeleting(filename)
    setDeleteError(null)
    try {
      await api.delete(`/kb/documents/${encodeURIComponent(filename)}`)
      await refresh()
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        `Could not delete ${filename}`
      setDeleteError(detail)
    } finally {
      setDeleting(null)
    }
  }, [refresh])

  const docs = stats?.documents ?? []
  const processing = jobs.filter((j) => j.status === 'processing').length
  const failed = jobs.filter((j) => j.status === 'failed').length

  return (
    <div className="main-content">
      <div className="page-header">
        <h1 className="page-title">Knowledge base</h1>
        <p className="page-sub">
          Documents every user's questions are answered from. Uploads here are chunked,
          embedded and added to the existing document set.
        </p>
      </div>

      <div className="page-body">
        <InstrumentationBanner
          missing={missing.filter((m) => m === 'ingestion_jobs')}
        />

        {loadError && (
          <div className="banner banner-warning">
            <span aria-hidden>⚠</span>
            <span><strong>Could not load the knowledge base.</strong> {loadError}</span>
          </div>
        )}

        {deleteError && (
          <div className="banner banner-warning">
            <span aria-hidden>⚠</span>
            <span><strong>Delete failed.</strong> {deleteError}</span>
          </div>
        )}

        <div className="stat-grid">
          <StatCard
            label="Chunks indexed"
            value={formatNumber(stats?.total_chunks ?? 0)}
            hint="searchable passages"
          />
          <StatCard
            label="Documents"
            value={formatNumber(stats?.total_documents ?? 0)}
            hint="in the shared corpus"
          />
          <StatCard
            label="Processing"
            value={processing}
            hint={processing > 0 ? 'chunking now' : 'idle'}
          />
          <StatCard
            label="Failed uploads"
            value={failed}
            hint={failed > 0 ? 'see the log below' : 'none'}
            tone={failed > 0 ? 'bad' : 'good'}
          />
        </div>

        {/* ── Upload ── */}
        <Card title="Add documents" sub="PDF, DOCX, XLSX, CSV, HTML, Markdown, text or images">
          <input
            ref={fileInput}
            type="file"
            accept={ACCEPTED}
            multiple
            style={{ display: 'none' }}
            onChange={(e) => { upload(e.target.files); e.target.value = '' }}
          />
          <div
            className={`dropzone ${dragging ? 'dragging' : ''}`}
            onClick={() => fileInput.current?.click()}
            onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => { e.preventDefault(); setDragging(false); upload(e.dataTransfer.files) }}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => { if (e.key === 'Enter') fileInput.current?.click() }}
          >
            <div className="dropzone-title">Drop files here, or click to choose</div>
            <div className="dropzone-sub">
              Each file is chunked and embedded into the shared knowledge base. Every user's
              questions can then be answered from it.
            </div>
          </div>

          {uploadError && (
            <div className="login-error" style={{ marginTop: 12, marginBottom: 0 }}>
              {uploadError}
            </div>
          )}
        </Card>

        {/* ── Ingestion status ── */}
        <Card title="Ingestion status" sub="Most recent uploads">
          {jobs.length === 0 ? (
            <div className="empty-state">
              {missing.includes('ingestion_jobs')
                ? 'Ingestion status is not being recorded yet — run migration 003.'
                : 'No uploads yet.'}
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table>
                <thead>
                  <tr>
                    <th>File</th>
                    <th>Status</th>
                    <th className="num">Chunks</th>
                    <th className="num">Size</th>
                    <th className="num">Took</th>
                    <th>Started</th>
                    <th>Detail</th>
                  </tr>
                </thead>
                <tbody>
                  {jobs.map((job) => (
                    <tr key={job.job_id}>
                      <td className="font-semibold" style={{ color: 'var(--text-primary)' }}>
                        {job.filename}
                      </td>
                      <td><StatusBadge status={job.status} /></td>
                      <td className="num">{job.chunks_added ? formatNumber(job.chunks_added) : '—'}</td>
                      <td className="num">{formatBytes(job.file_size_bytes ?? undefined)}</td>
                      <td className="num">
                        {job.duration_ms ? `${(job.duration_ms / 1000).toFixed(1)}s` : '—'}
                      </td>
                      <td style={{ whiteSpace: 'nowrap' }}>{formatWhen(job.started_at)}</td>
                      <td className="truncate" style={{ color: job.error_message ? 'var(--error)' : undefined }}>
                        {job.error_message ?? (job.status === 'processing' ? 'Chunking…' : 'Indexed')}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        {/* ── Corpus ── */}
        <div className="charts-row wide">
          <Card title="Documents" sub={`${docs.length} in the knowledge base`}>
            {loading ? (
              <div className="loading-state">Loading…</div>
            ) : docs.length === 0 ? (
              <div className="empty-state">The knowledge base is empty. Upload a document to begin.</div>
            ) : (
              <div className="overflow-x-auto">
                <table>
                  <thead>
                    <tr>
                      <th>Document</th>
                      <th>Type</th>
                      <th className="num">Chunks</th>
                      <th>Added</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {docs.map((doc) => (
                      <tr key={doc.doc_id}>
                        <td className="truncate" title={doc.filename}
                            style={{ color: 'var(--text-primary)', fontWeight: 500 }}>
                          {doc.filename}
                        </td>
                        <td><span className="badge">{doc.doc_type || '—'}</span></td>
                        <td className="num">{formatNumber(doc.chunk_count)}</td>
                        <td style={{ whiteSpace: 'nowrap' }}>{formatWhen(doc.ingested_at)}</td>
                        <td style={{ textAlign: 'right' }}>
                          <button
                            className="btn btn-danger"
                            disabled={deleting === doc.filename}
                            onClick={() => remove(doc.filename)}
                          >
                            {deleting === doc.filename ? 'Deleting…' : 'Delete'}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          <Card title="Chunks per document" sub="Top 10 by size">
            {docs.length > 0 ? (
              <BreakdownChart
                data={docs.slice(0, 10).map((d) => ({
                  ...d,
                  label: d.filename.length > 22 ? `${d.filename.slice(0, 20)}…` : d.filename,
                }))}
                categoryKey="label"
                valueKey="chunk_count"
                height="tall"
              />
            ) : (
              <NoData label="No documents indexed" />
            )}
          </Card>
        </div>
      </div>
    </div>
  )
}

export default KnowledgeBasePage
