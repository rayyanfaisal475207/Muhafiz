import { useState, useEffect } from 'react';
import { useProjectStore, type Project } from '../../store/projectStore';
import { useModalA11y } from '../../hooks/useModalA11y';

interface ProjectSettingsModalProps {
  isOpen: boolean;
  onClose: () => void;
  editProject: Project | null;
}

export function ProjectSettingsModal({ isOpen, onClose, editProject }: ProjectSettingsModalProps) {
  const { createProject, updateProject } = useProjectStore();
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [domainContext, setDomainContext] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (isOpen) {
      if (editProject) {
        setName(editProject.name);
        setDescription(editProject.description || '');
        setDomainContext(editProject.domain_context || '');
      } else {
        setName('');
        setDescription('');
        setDomainContext('');
      }
      setError(null);
    }
  }, [isOpen, editProject]);

  const dialogRef = useModalA11y(isOpen, onClose);

  if (!isOpen) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setIsLoading(true);

    try {
      if (editProject) {
        await updateProject(editProject.id, {
          name,
          description,
          domain_context: domainContext
        });
      } else {
        await createProject({
          name,
          description,
          domain_context: domainContext
        });
      }
      onClose();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to save project');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="project-modal-title"
        tabIndex={-1}
        className="rounded-lg w-full max-w-lg overflow-hidden" style={{ background: 'var(--bg-surface)', border: '1px solid var(--border)', boxShadow: 'var(--shadow-lg)' }}
      >
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)]" style={{ background: 'var(--bg-surface-2)' }}>
          <h2 id="project-modal-title" className="text-xl font-bold text-[var(--text-primary)]">
            {editProject ? 'Edit Project' : 'New Project'}
          </h2>
          <button onClick={onClose} aria-label="Close" className="p-1 rounded-xs text-[var(--text-muted)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-surface-3)] transition-colors">
            <svg className="w-6 h-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
        
        <form onSubmit={handleSubmit} className="p-6">
          {error && (
            <div
              className="mb-4 p-3 rounded text-sm"
              style={{
                background: 'var(--error-soft)',
                border: '1px solid color-mix(in srgb, var(--error) 30%, transparent)',
                color: 'var(--error)',
              }}
            >
              {error}
            </div>
          )}

          <div className="space-y-4">
            <div>
              <label htmlFor="project-name" className="block text-[13px] font-medium text-[var(--text-secondary)] mb-1">
                Project Name *
              </label>
              <input
                id="project-name"
                type="text"
                required
                className="w-full rounded-sm px-3 py-2 text-[15px] bg-[var(--bg-surface)] text-[var(--text-primary)] border border-[var(--border-strong)] transition-colors hover:border-[var(--border-hover)] focus:outline-none focus:border-[var(--accent)]"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="E.g., Tech Corp Restructuring"
              />
            </div>

            <div>
              <label htmlFor="project-description" className="block text-[13px] font-medium text-[var(--text-secondary)] mb-1">
                Description
              </label>
              <input
                id="project-description"
                type="text"
                className="w-full rounded-sm px-3 py-2 text-[15px] bg-[var(--bg-surface)] text-[var(--text-primary)] border border-[var(--border-strong)] transition-colors hover:border-[var(--border-hover)] focus:outline-none focus:border-[var(--accent)]"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="Optional description"
              />
            </div>

            <div>
              <label htmlFor="project-domain-context" className="block text-[13px] font-medium text-[var(--text-secondary)] mb-1 flex justify-between items-center">
                <span>Domain Context</span>
                <span className="text-xs text-[var(--text-faint)] font-normal">Injected into LLM memory</span>
              </label>
              <textarea
                id="project-domain-context"
                className="w-full h-32 rounded-sm px-3 py-2 text-[15px] bg-[var(--bg-surface)] text-[var(--text-primary)] border border-[var(--border-strong)] transition-colors hover:border-[var(--border-hover)] focus:outline-none focus:border-[var(--accent)] resize-none"
                value={domainContext}
                onChange={(e) => setDomainContext(e.target.value)}
                placeholder="Add background facts, terminology, or constraints specific to this project..."
              />
            </div>
          </div>

          <div className="mt-8 flex justify-end gap-3">
            <button
              type="button"
              onClick={onClose}
              className="btn-ghost px-4 py-2 text-sm"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={isLoading || !name.trim()}
              className="btn-accent px-4 py-2 text-sm"
            >
              {isLoading ? 'Saving...' : 'Save Project'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
