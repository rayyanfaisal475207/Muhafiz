// ============================================================
// StatusIcons — minimal line icons for generation phases.
//
// Rules for this set:
//   * 1.5px strokes, round caps, no fills, no gradients.
//   * Each icon animates the part that *means* something (the search
//     sweep, the globe's meridian, the pen's stroke) rather than
//     spinning the whole glyph. Motion should read as the work being
//     done, not as "please wait".
//   * All motion is CSS-driven and disabled under prefers-reduced-motion.
// ============================================================

interface IconProps {
  className?: string;
  /** Active phase icons animate; completed/idle ones sit still. */
  animate?: boolean;
}

const BASE = 'w-[15px] h-[15px]';

function Svg({ className = '', children }: { className?: string; children: React.ReactNode }) {
  return (
    <svg
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`${BASE} ${className}`}
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

/** Understanding the question — a sparkle "reading" the prompt. */
export function SparkIcon({ className, animate }: IconProps) {
  return (
    <Svg className={className}>
      <path d="M10 2.5 11.6 7 16 8.6 11.6 10.2 10 14.6 8.4 10.2 4 8.6 8.4 7 10 2.5Z">
        {animate && (
          <animate attributeName="opacity" values="1;0.45;1" dur="1.8s" repeatCount="indefinite" />
        )}
      </path>
      <path d="M15.5 13.5 16 15l1.5.5L16 16l-.5 1.5L15 16l-1.5-.5L15 15l.5-1.5Z" opacity={0.55}>
        {animate && (
          <animate attributeName="opacity" values="0.2;0.9;0.2" dur="1.8s" begin="0.6s" repeatCount="indefinite" />
        )}
      </path>
    </Svg>
  );
}

/** Choosing an approach — a branching route. */
export function RouteIcon({ className, animate }: IconProps) {
  return (
    <Svg className={className}>
      <circle cx="4.5" cy="10" r="1.75" />
      <circle cx="15.5" cy="5" r="1.75" />
      <circle cx="15.5" cy="15" r="1.75" />
      <path d="M6.25 10h2.5c1.2 0 2-.7 2.6-1.6l.9-1.4" strokeDasharray="14" style={animate ? { ['--dash' as string]: '14', animation: 'stroke-draw 1.5s ease-in-out infinite' } : undefined} />
      <path d="M6.25 10h2.5c1.2 0 2 .7 2.6 1.6l.9 1.4" opacity={0.35} />
    </Svg>
  );
}

/** Searching the knowledge base — a magnifier sweeping over a page. */
export function SearchIcon({ className, animate }: IconProps) {
  return (
    <Svg className={className}>
      <circle cx="8.75" cy="8.75" r="5.25" />
      <path d="m12.75 12.75 4 4" />
      <path d="M6.5 8.75h4.5" opacity={0.6} strokeDasharray="4.5" style={animate ? { ['--dash' as string]: '4.5', animation: 'stroke-draw 1.3s ease-in-out infinite' } : undefined} />
    </Svg>
  );
}

/** Searching the web — a globe whose meridian drifts. */
export function GlobeIcon({ className, animate }: IconProps) {
  return (
    <Svg className={className}>
      <circle cx="10" cy="10" r="7" />
      <path d="M3 10h14" />
      <ellipse cx="10" cy="10" rx="3.1" ry="7">
        {animate && (
          <animate attributeName="rx" values="3.1;0.7;3.1" dur="2.6s" repeatCount="indefinite" />
        )}
      </ellipse>
    </Svg>
  );
}

/** Ranking sources — sliders settling into order. */
export function RankIcon({ className, animate }: IconProps) {
  return (
    <Svg className={className}>
      <path d="M4 5.5h12" opacity={0.45} />
      <path d="M4 10h12" opacity={0.45} />
      <path d="M4 14.5h12" opacity={0.45} />
      <circle cx="7" cy="5.5" r="1.6" fill="currentColor" stroke="none">
        {animate && <animate attributeName="cx" values="7;13;7" dur="2.4s" repeatCount="indefinite" />}
      </circle>
      <circle cx="12.5" cy="10" r="1.6" fill="currentColor" stroke="none">
        {animate && <animate attributeName="cx" values="12.5;6;12.5" dur="2.4s" begin="0.3s" repeatCount="indefinite" />}
      </circle>
      <circle cx="8.5" cy="14.5" r="1.6" fill="currentColor" stroke="none">
        {animate && <animate attributeName="cx" values="8.5;14;8.5" dur="2.4s" begin="0.6s" repeatCount="indefinite" />}
      </circle>
    </Svg>
  );
}

/** Reading results — an open document with lines being scanned. */
export function ReadIcon({ className, animate }: IconProps) {
  return (
    <Svg className={className}>
      <path d="M4 4.5h5c.8 0 1.5.7 1.5 1.5v9.5c0-.8-.7-1.5-1.5-1.5H4V4.5Z" />
      <path d="M16 4.5h-5c-.8 0-1.5.7-1.5 1.5v9.5c0-.8.7-1.5 1.5-1.5h5V4.5Z" />
      <path d="M5.6 7.6h3" opacity={0.6}>
        {animate && <animate attributeName="opacity" values="0.15;0.9;0.15" dur="1.6s" repeatCount="indefinite" />}
      </path>
      <path d="M11.6 9.6h3" opacity={0.6}>
        {animate && <animate attributeName="opacity" values="0.15;0.9;0.15" dur="1.6s" begin="0.5s" repeatCount="indefinite" />}
      </path>
    </Svg>
  );
}

/** Writing the response — a pen drawing its own stroke. */
export function PenIcon({ className, animate }: IconProps) {
  return (
    <Svg className={className}>
      <path d="M13.4 3.9a1.9 1.9 0 0 1 2.7 2.7L7.9 14.8l-3.4.7.7-3.4 8.2-8.2Z" />
      <path
        d="M4.5 17h11"
        opacity={0.7}
        strokeDasharray="11"
        style={animate ? { ['--dash' as string]: '11', animation: 'stroke-draw 1.6s ease-in-out infinite' } : undefined}
      />
    </Svg>
  );
}

/** Building a file — a document assembling itself. */
export function FileIcon({ className, animate }: IconProps) {
  return (
    <Svg className={className}>
      <path d="M5 3.5h6l4 4v9a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-12a1 1 0 0 1 1-1Z" />
      <path d="M11 3.5v4h4" />
      <path d="M6.5 11h7" opacity={0.6}>
        {animate && <animate attributeName="opacity" values="0.1;0.9;0.1" dur="1.5s" repeatCount="indefinite" />}
      </path>
      <path d="M6.5 13.8h4.5" opacity={0.6}>
        {animate && <animate attributeName="opacity" values="0.1;0.9;0.1" dur="1.5s" begin="0.4s" repeatCount="indefinite" />}
      </path>
    </Svg>
  );
}

/** Checking citations — a verified mark. */
export function VerifyIcon({ className, animate }: IconProps) {
  return (
    <Svg className={className}>
      <path d="M10 2.8 16 5v4.6c0 3.4-2.4 6.4-6 7.6-3.6-1.2-6-4.2-6-7.6V5l6-2.2Z" />
      <path
        d="m7.4 9.9 1.9 1.9 3.4-3.6"
        strokeDasharray="8"
        style={animate ? { ['--dash' as string]: '8', animation: 'stroke-draw 1.5s ease-in-out infinite' } : undefined}
      />
    </Svg>
  );
}

/** Saving to memory — a bookmark. */
export function SaveIcon({ className, animate }: IconProps) {
  return (
    <Svg className={className}>
      <path d="M5.5 3.5h9v13l-4.5-3.2L5.5 16.5v-13Z">
        {animate && <animate attributeName="opacity" values="1;0.5;1" dur="1.6s" repeatCount="indefinite" />}
      </path>
    </Svg>
  );
}

/** Something failed. */
export function AlertIcon({ className }: IconProps) {
  return (
    <Svg className={className}>
      <circle cx="10" cy="10" r="7" />
      <path d="M10 6.5v4.2" />
      <path d="M10 13.4h.01" />
    </Svg>
  );
}

/** A completed phase in the collapsed timeline. */
export function CheckIcon({ className }: IconProps) {
  return (
    <Svg className={className}>
      <path d="m4.8 10.4 3.1 3.1 7.3-7.3" />
    </Svg>
  );
}

/** A skipped phase. */
export function SkipIcon({ className }: IconProps) {
  return (
    <Svg className={className}>
      <path d="M5 15 15 5" opacity={0.7} />
    </Svg>
  );
}

/** Chevron for expand/collapse. */
export function ChevronIcon({ className }: IconProps) {
  return (
    <Svg className={className}>
      <path d="m7.5 4.5 5.5 5.5-5.5 5.5" />
    </Svg>
  );
}
