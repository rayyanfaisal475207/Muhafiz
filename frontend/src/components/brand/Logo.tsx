// ============================================================
// Logo — the Muhafiz mark and lockup, as inline SVG.
//
// Inline (not <img src="/brand/...">) so the mark inherits the current
// theme's colors via currentColor and never flashes in late. The files in
// /public/brand are the canonical hand-off assets for decks, favicons,
// and anything outside this app.
// ============================================================

interface MarkProps {
  className?: string;
  /** 'tile' = navy tile with knocked-out spark (app icon).
   *  'glyph' = the spark alone, inheriting currentColor. */
  variant?: 'tile' | 'glyph';
}

/** The four-point spark: Muhafiz's core geometry (unchanged from before the rename). */
const SPARK_PATH =
  'M32 12 C33.1 21.4 42.6 30.9 52 32 C42.6 33.1 33.1 42.6 32 52 ' +
  'C30.9 42.6 21.4 33.1 12 32 C21.4 30.9 30.9 21.4 32 12Z';

export function LogoMark({ className = 'w-8 h-8', variant = 'tile' }: MarkProps) {
  if (variant === 'glyph') {
    return (
      <svg viewBox="0 0 64 64" className={className} fill="currentColor" role="img" aria-label="Muhafiz">
        <path d="M32 8 C33.3 19.3 44.7 30.7 56 32 C44.7 33.3 33.3 44.7 32 56 C30.7 44.7 19.3 33.3 8 32 C19.3 30.7 30.7 19.3 32 8Z" />
      </svg>
    );
  }

  return (
    <svg viewBox="0 0 64 64" className={className} role="img" aria-label="Muhafiz">
      <path
        d="M0 0h50a14 14 0 0 1 14 14v36a14 14 0 0 1-14 14H14A14 14 0 0 1 0 50V0Z"
        fill="var(--accent)"
      />
      <path d={SPARK_PATH} fill="var(--bg-base)" />
    </svg>
  );
}

interface LockupProps {
  className?: string;
  /** Height of the mark; the wordmark scales with it. */
  size?: 'sm' | 'md';
}

export function LogoLockup({ className = '', size = 'md' }: LockupProps) {
  const markSize = size === 'sm' ? 'w-7 h-7' : 'w-8 h-8';
  const textSize = size === 'sm' ? 'text-[15px]' : 'text-[17px]';

  return (
    <span className={`inline-flex items-center gap-2.5 ${className}`}>
      <LogoMark className={markSize} />
      <span
        className={`${textSize} font-semibold tracking-[-0.015em] select-none`}
        style={{ color: 'var(--text-primary)' }}
      >
        Muhafiz
      </span>
    </span>
  );
}
