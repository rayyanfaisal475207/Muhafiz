import type { Source } from '../../types';

interface Props {
  file: Source;
}

export function FileResultCard({ file }: Props) {
  return (
    <div 
      className="flex items-center justify-between p-3 rounded-xl border glass-panel hover:shadow-lg transition-all duration-200 group" 
      style={{ borderColor: 'var(--border)' }}
    >
      <div className="flex items-center gap-3">
        <div className="w-10 h-10 rounded-lg flex items-center justify-center bg-accent/10 text-accent group-hover:scale-105 transition-transform duration-200">
          <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
          </svg>
        </div>
        <div>
          <p className="text-sm font-medium text-[var(--text-primary)] group-hover:text-accent transition-colors duration-200">{file.filename}</p>
          <p className="text-[11px] text-[var(--text-muted)] uppercase tracking-wider">{file.type || 'DOCUMENT'}</p>
        </div>
      </div>
      <a
        href={`${import.meta.env.VITE_API_URL || 'http://localhost:8000/api'}/files/${file.file_id}/download`}
        download
        className="px-4 py-1.5 rounded-lg text-xs font-semibold bg-accent text-[var(--accent-on)] hover:bg-accent-hover transition-all duration-200 shadow-sm hover:shadow-md hover:-translate-y-0.5 active:translate-y-0"
        target="_blank"
        rel="noreferrer"
      >
        Download
      </a>
    </div>
  );
}
