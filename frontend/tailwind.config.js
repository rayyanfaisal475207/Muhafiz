/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'sans-serif'],
        mono: ['JetBrains Mono', 'Fira Code', 'ui-monospace', 'monospace'],
      },
      // Every color resolves to a token in index.css — no raw hex in components.
      colors: {
        base:      'var(--bg-base)',
        surface:   'var(--bg-surface)',
        surface2:  'var(--bg-surface-2)',
        surface3:  'var(--bg-surface-3)',

        ink:       'var(--text-primary)',
        body:      'var(--text-secondary)',
        muted:     'var(--text-muted)',
        faint:     'var(--text-faint)',

        accent: {
          DEFAULT: 'var(--accent)',
          hover:   'var(--accent-hover)',
          active:  'var(--accent-active)',
          on:      'var(--accent-on)',
          soft:    'var(--accent-soft)',
          border:  'var(--accent-border)',
        },

        success:  'var(--success)',
        warning:  'var(--warning)',
        error:    'var(--error)',
        info:     'var(--info)',
      },
      borderColor: {
        DEFAULT: 'var(--border)',
        strong:  'var(--border-strong)',
        hover:   'var(--border-hover)',
      },
      borderRadius: {
        xs:   'var(--radius-xs)',
        sm:   'var(--radius-sm)',
        DEFAULT: 'var(--radius)',
        md:   'var(--radius)',
        lg:   'var(--radius-lg)',
        xl:   'var(--radius-xl)',
        pill: 'var(--radius-pill)',
      },
      boxShadow: {
        xs: 'var(--shadow-xs)',
        sm: 'var(--shadow-sm)',
        md: 'var(--shadow-md)',
        lg: 'var(--shadow-lg)',
      },
      ringColor: {
        DEFAULT: 'var(--accent-ring)',
      },
      animation: {
        'pulse-dot':      'pulse-dot 1.4s ease-in-out infinite',
        'fade-in':        'fade-in 0.25s ease-out both',
        'spin-slow':      'spin-slow 1.6s linear infinite',
        'status-icon':    'status-icon-in 0.28s cubic-bezier(0.2,0.7,0.3,1) both',
        'status-breathe': 'status-breathe 1.9s ease-in-out infinite',
        'status-halo':    'status-halo 1.8s ease-out infinite',
        'stroke-draw':    'stroke-draw 1.4s ease-in-out infinite',
        'token-in':       'token-in 0.18s ease-out both',
      },
      keyframes: {
        'pulse-dot': {
          '0%, 100%': { opacity: '1', transform: 'scale(1)' },
          '50%':      { opacity: '0.4', transform: 'scale(0.85)' },
        },
        'fade-in': {
          from: { opacity: '0', transform: 'translateY(6px)' },
          to:   { opacity: '1', transform: 'translateY(0)' },
        },
        'spin-slow': {
          from: { transform: 'rotate(0deg)' },
          to:   { transform: 'rotate(360deg)' },
        },
        'status-icon-in': {
          from: { opacity: '0', transform: 'translateY(3px) scale(0.9)' },
          to:   { opacity: '1', transform: 'translateY(0) scale(1)' },
        },
        'status-breathe': {
          '0%, 100%': { opacity: '1' },
          '50%':      { opacity: '0.55' },
        },
        'status-halo': {
          '0%':   { transform: 'scale(0.7)', opacity: '0.55' },
          '70%':  { transform: 'scale(1.5)', opacity: '0' },
          '100%': { transform: 'scale(1.5)', opacity: '0' },
        },
        'stroke-draw': {
          from: { strokeDashoffset: '24' },
          to:   { strokeDashoffset: '0' },
        },
        'token-in': {
          from: { opacity: '0.35' },
          to:   { opacity: '1' },
        },
      },
    },
  },
  plugins: [],
}
