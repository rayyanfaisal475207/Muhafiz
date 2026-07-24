// ============================================================
// PipelineStepCard — one step in the live pipeline trace.
//
// This is the engineer's view (step names, timings, retries), so it stays
// literal where the chat's GenerationStatus is human. Both share the same
// visual language: navy = in progress / selected, warm neutral = idle.
// ============================================================

import type { ReactElement } from 'react';
import type { PipelineStep } from '../../types';
import { formatMs } from '../../lib/utils';

interface Props {
  step: PipelineStep;
}

// Status → visual treatment. Colors come from tokens, never raw hex.
const STATUS_CONFIG: Record<
  PipelineStep['status'],
  { bg: string; border: string; color: string; opacity?: number; icon: ReactElement }
> = {
  waiting: {
    bg: 'transparent',
    border: 'var(--border)',
    color: 'var(--text-faint)',
    opacity: 0.75,
    icon: <DashIcon />,
  },
  active: {
    bg: 'var(--accent-soft)',
    border: 'var(--accent-border)',
    color: 'var(--accent)',
    icon: <PulseIcon />,
  },
  done: {
    bg: 'var(--bg-surface)',
    border: 'var(--border)',
    color: 'var(--success)',
    icon: <CheckIcon />,
  },
  skipped: {
    bg: 'transparent',
    border: 'var(--border)',
    color: 'var(--text-faint)',
    opacity: 0.6,
    icon: <SkipIcon />,
  },
  error: {
    bg: 'var(--error-soft)',
    border: 'color-mix(in srgb, var(--error) 30%, transparent)',
    color: 'var(--error)',
    icon: <AlertIcon />,
  },
  retry: {
    bg: 'var(--warning-soft)',
    border: 'color-mix(in srgb, var(--warning) 30%, transparent)',
    color: 'var(--warning)',
    icon: <RetryIcon />,
  },
};

export function PipelineStepCard({ step }: Props) {
  const cfg = STATUS_CONFIG[step.status];
  const isMuted = step.status === 'waiting' || step.status === 'skipped';

  return (
    <div
      className="flex items-start gap-2.5 px-3 py-2.5 rounded-sm border transition-all duration-300 animate-fade-in"
      style={{
        background: cfg.bg,
        borderColor: cfg.border,
        opacity: cfg.opacity,
      }}
    >
      {/* Status icon */}
      <div className="shrink-0 mt-px" style={{ color: cfg.color }}>
        {cfg.icon}
      </div>

      {/* Content */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center justify-between gap-2">
          <span
            className="text-xs font-medium"
            style={{ color: isMuted ? 'var(--text-muted)' : 'var(--text-primary)' }}
          >
            {step.label}
            {step.status === 'retry' && step.retryNum !== undefined && (
              <span
                className="ml-1.5 text-[10px] px-1.5 py-0.5 rounded-pill"
                style={{ background: 'var(--warning-soft)', color: 'var(--warning)' }}
              >
                retry #{step.retryNum}
              </span>
            )}
          </span>
          {step.ms !== undefined && (
            <span className="text-[10px] tabular-nums shrink-0" style={{ color: 'var(--text-faint)' }}>
              {formatMs(step.ms)}
            </span>
          )}
        </div>

        {step.detail && step.status !== 'waiting' && (
          <p
            className="text-[11px] mt-0.5 truncate"
            style={{ color: 'var(--text-muted)' }}
            title={step.detail}
          >
            {step.detail}
          </p>
        )}

        {/* Active: a navy sweep, not a fake percentage */}
        {step.status === 'active' && (
          <div
            className="mt-1.5 h-0.5 rounded-pill overflow-hidden"
            style={{ background: 'var(--accent-soft)' }}
          >
            <div
              className="h-full rounded-pill"
              style={{
                background:
                  'linear-gradient(90deg, transparent, var(--accent), transparent)',
                backgroundSize: '200% 100%',
                animation: 'shimmer 1.5s infinite linear',
              }}
            />
          </div>
        )}
      </div>
    </div>
  );
}

// ── Icons — line style, 1.5px strokes, matching the chat status set ──────────

const iconProps = {
  viewBox: '0 0 20 20',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.5,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  className: 'w-3.5 h-3.5',
  'aria-hidden': true,
};

function DashIcon() {
  return (
    <svg {...iconProps}>
      <path d="M5 10h10" opacity={0.6} />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg {...iconProps}>
      <path d="m4.8 10.4 3.1 3.1 7.3-7.3" />
    </svg>
  );
}

function AlertIcon() {
  return (
    <svg {...iconProps}>
      <circle cx="10" cy="10" r="7" />
      <path d="M10 6.5v4.2" />
      <path d="M10 13.4h.01" />
    </svg>
  );
}

function SkipIcon() {
  return (
    <svg {...iconProps}>
      <path d="M5 15 15 5" opacity={0.7} />
    </svg>
  );
}

function RetryIcon() {
  return (
    <svg {...iconProps}>
      <path d="M16 10a6 6 0 1 1-1.8-4.3" />
      <path d="M16 3.5V7h-3.5" />
    </svg>
  );
}

/** Active: a ring that breathes rather than a spinner that races. */
function PulseIcon() {
  return (
    <svg {...iconProps} className="w-3.5 h-3.5">
      <circle cx="10" cy="10" r="6.5" opacity={0.3} />
      <circle cx="10" cy="10" r="2.6" fill="currentColor" stroke="none">
        <animate attributeName="r" values="2.6;4.2;2.6" dur="1.6s" repeatCount="indefinite" />
        <animate attributeName="opacity" values="1;0.45;1" dur="1.6s" repeatCount="indefinite" />
      </circle>
    </svg>
  );
}
