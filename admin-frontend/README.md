# Muhafiz Admin Console

The admin/operator frontend for the Muhafiz Evidence Intelligence Platform — a
React + TypeScript + Vite app for platform-admin/station-admin/supervisor
users to manage cases, review the knowledge base, monitor pipeline runs and
errors, and audit system activity. Separate from the main investigator-facing
chat app in [`../frontend`](../frontend).

## Running locally

Requires the backend API running (see the repo root `README.md` for backend
setup — Postgres + Apache AGE, `.env`, etc). By default the dev server proxies
`/api` to `http://127.0.0.1:8000` (see `vite.config.ts`).

```bash
npm install
npm run dev
```

Opens on `http://localhost:5174`.

## Scripts

| Command           | Purpose                                   |
| ------------------ | ------------------------------------------ |
| `npm run dev`       | Start the Vite dev server (port 5174)      |
| `npm run build`     | Type-check (`tsc -b`) and production build |
| `npm run preview`   | Preview the production build locally       |
| `npm run lint`      | Run Oxlint                                  |
| `npm test`          | Run the Vitest suite once                   |
| `npm run test:watch`| Run Vitest in watch mode                    |

## Notes

- Auth is a JWT in an HttpOnly cookie, shared with the backend's session
  model — log in via the console's own login page, not the main chat app's.
- Access to most pages is role-gated (`platform-admin` / `station-admin` /
  `supervisor`); see `src/App.tsx` and `src/AuthContext.tsx` for the role
  hierarchy.
