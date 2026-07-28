// ============================================================
// Utility helpers
// ============================================================

/** Generate a random session ID (UUID v4) */
export function generateSessionId(): string {
  return crypto.randomUUID();
}

/** Format milliseconds to a readable string */
export function formatMs(ms: number | undefined): string {
  if (ms === undefined || ms === null) return '';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

/** Map a file extension to a Tailwind text-color class */
export function getFileTypeColor(ext: string): string {
  const map: Record<string, string> = {
    pdf:  'text-red-400',
    csv:  'text-green-400',
    xlsx: 'text-green-400',
    xls:  'text-green-400',
    html: 'text-blue-400',
    htm:  'text-blue-400',
    docx: 'text-blue-400',
    doc:  'text-blue-400',
    jpg:  'text-purple-400',
    jpeg: 'text-purple-400',
    png:  'text-purple-400',
    webp: 'text-purple-400',
    gif:  'text-purple-400',
    txt:  'text-gray-400',
    md:   'text-gray-400',
  };
  return map[ext.toLowerCase()] ?? 'text-gray-400';
}

/** Map a file extension to a badge bg class */
export function getFileTypeBadgeBg(ext: string): string {
  const map: Record<string, string> = {
    pdf:  'bg-red-500/15 text-red-400 ring-red-500/25',
    csv:  'bg-green-500/15 text-green-400 ring-green-500/25',
    xlsx: 'bg-green-500/15 text-green-400 ring-green-500/25',
    xls:  'bg-green-500/15 text-green-400 ring-green-500/25',
    html: 'bg-blue-500/15 text-blue-400 ring-blue-500/25',
    htm:  'bg-blue-500/15 text-blue-400 ring-blue-500/25',
    docx: 'bg-blue-500/15 text-blue-400 ring-blue-500/25',
    doc:  'bg-blue-500/15 text-blue-400 ring-blue-500/25',
    jpg:  'bg-purple-500/15 text-purple-400 ring-purple-500/25',
    jpeg: 'bg-purple-500/15 text-purple-400 ring-purple-500/25',
    png:  'bg-purple-500/15 text-purple-400 ring-purple-500/25',
    webp: 'bg-purple-500/15 text-purple-400 ring-purple-500/25',
    gif:  'bg-purple-500/15 text-purple-400 ring-purple-500/25',
    txt:  'bg-gray-500/15 text-gray-400 ring-gray-500/25',
    md:   'bg-gray-500/15 text-gray-400 ring-gray-500/25',
  };
  return map[ext.toLowerCase()] ?? 'bg-gray-500/15 text-gray-400 ring-gray-500/25';
}

/** Get a file type emoji/icon string */
export function getFileTypeIcon(ext: string): string {
  const map: Record<string, string> = {
    pdf:  '📄',
    csv:  '📊',
    xlsx: '📊',
    xls:  '📊',
    html: '🌐',
    htm:  '🌐',
    docx: '📝',
    doc:  '📝',
    jpg:  '🖼️',
    jpeg: '🖼️',
    png:  '🖼️',
    webp: '🖼️',
    gif:  '🖼️',
    txt:  '📃',
    md:   '📃',
  };
  return map[ext.toLowerCase()] ?? '📁';
}

/** Format a byte count to a readable size */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Format an ISO date string to a short readable date */
export function formatDate(iso: string): string {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleDateString('en-US', {
      month: 'short', day: 'numeric', year: 'numeric',
    });
  } catch {
    return iso;
  }
}
