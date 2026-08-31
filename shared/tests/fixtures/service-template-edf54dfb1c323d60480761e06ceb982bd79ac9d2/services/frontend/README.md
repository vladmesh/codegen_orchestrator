# Frontend (Astro + React)

This directory hosts a lightweight Astro project that can render React
components. The template keeps dependencies to a minimum so that you can
bootstrap pages, islands, or a full SPA depending on your needs.

## Getting started

1. Install dependencies (Astro, React, linting tooling):
   ```bash
   npm install
   ```
2. Run the local development server:
   ```bash
   npm run dev
   ```
   The server listens on port `4321` by default.
3. Build the static output:
   ```bash
   npm run build
   ```
4. Preview the production build locally:
   ```bash
   npm run preview
   ```

## Docker Compose

The standard base and dev layers define the frontend. Local development adds the local-port layer:

```bash
make dev-start svc=frontend
```

`FRONTEND_PORT` publishes container port `4321`. Update `package.json` or add Astro configuration
files as you flesh out this project.
