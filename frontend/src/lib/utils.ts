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

/** Format a byte count to a readable size */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
