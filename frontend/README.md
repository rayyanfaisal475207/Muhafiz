# Muhafiz Chat

The main investigator-facing chat frontend for the Muhafiz Evidence
Intelligence Platform — a React + TypeScript + Vite app where investigators
converse with the assistant over case evidence, switch between cases/
projects, and review citations. Separate from the operator/admin dashboard
in [`../admin-frontend`](../admin-frontend).

## Running locally

Requires the backend API running (see the repo root `README.md` for backend
setup — Postgres + Apache AGE, `.env`, etc). The dev server proxies `/api` to
`http://127.0.0.1:8000`, stripping the `/api` prefix (see `vite.config.ts`).

```bash
npm install
npm run dev
```

Opens on `http://localhost:5173`.

## Scripts

| Command              | Purpose                                    |
| --------------------- | ------------------------------------------- |
| `npm run dev`         | Start the Vite dev server (port 5173)       |
| `npm run build`       | Type-check (`tsc -b`) and production build  |
| `npm run preview`     | Preview the production build locally        |
| `npm run lint`        | Run Oxlint                                   |
| `npm test`            | Run the Vitest suite once                    |
| `npm run test:watch`  | Run Vitest in watch mode                     |

## Notes

- Auth is a JWT in an HttpOnly cookie, shared with the backend's session
  model — log in via this app's own login page, not the admin console's.
- State (active case/project, chat sessions, streaming) lives in Zustand
  stores under `src/store/`; SSE streaming and the REST client live in
  `src/lib/api.ts`.
