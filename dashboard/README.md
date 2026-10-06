# Governed dashboard

See [dashboard setup and roles](../docs/dashboard.md).

Pinned Next.js 16.3.8, React 19.3.0, TypeScript 7.0.2 and Playwright 1.63.0. Use Node 22 and pnpm 11.19.0. `pnpm install --frozen-lockfile`, `pnpm typecheck`, `pnpm build`, `pnpm start`. The admin service must be available at `ADMIN_API_URL` (server-only; default localhost:8001).

For browser acceptance, run the root `scripts/seed_e2e.py`, then `pnpm test`. Generated test credentials and browser artifacts are gitignored. No provider or bootstrap secret is bundled into browser assets.
