import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import { App } from './App.tsx'
import { applyTheme, resolveInitialTheme } from './lib/theme'

// Apply the persisted (or OS-preferred) theme before first paint so there is
// no flash of the wrong theme.
applyTheme(resolveInitialTheme())

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
