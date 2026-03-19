# #1025 Admin UI: Settings page (config + prompt editor)

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Brainstorm bs-d124d343 (Admin Panel v2) defined Phase 4 "Admin UI: Settings page". Prerequisite #1020 (SystemConfig model + API + ConfigStore) is done — the backend is fully implemented:

- `GET /api/system-configs/?category=X` returns configs grouped by category
- `PATCH /api/system-configs/{key}` updates a config value
- `GET /api/agent-configs/` returns all agent configs (prompts, model settings)
- `PATCH /api/agent-configs/{id}` updates agent config (auto-increments version)

The admin SPA is a React 19 + Tailwind v4 + TanStack Query app. Currently has 12 pages, no Settings page. Custom dark theme, no component library. Inline edit pattern exists in TaskDetailPage (useState toggle + useMutation). API wrapper in `lib/api.ts` (fetch-based).

This task adds a Settings page with two tabs:
1. **System Configs** — table grouped by category, inline edit per row, Save button
2. **Agent Configs (Prompts)** — list of agent configs with expandable prompt editor, model/temperature fields

## Steps

1. [ ] Add TypeScript types for SystemConfig and AgentConfig
   - **Input**: `src/types/api.ts`
   - **Output**: `SystemConfig` and `AgentConfig` interfaces matching API schemas
   - **Test**: TypeScript compilation passes, types match API response shape

2. [ ] Create SettingsPage component with tabs and system config table
   - **Input**: `src/pages/SettingsPage.tsx` (new file)
   - **Output**: Page with two tabs ("System Configs", "Agent Configs"). First tab shows configs fetched from `GET /api/system-configs/`, grouped by category with collapsible sections. Each row: key, description, current value (read-only by default), Edit button
   - **Test**: Page renders, data loads from API, categories are grouped correctly

3. [ ] Add inline edit + save for system config values
   - **Input**: `src/pages/SettingsPage.tsx`
   - **Output**: Click Edit on a row → value becomes editable input (number input for numeric, text input for strings). Save button calls `PATCH /api/system-configs/{key}` with `updated_by: "admin"`. Cancel reverts. Success invalidates query cache. Error shows inline message
   - **Test**: Edit → change value → Save works. Cancel reverts. API error shows feedback

4. [ ] Create Agent Configs tab with prompt editor
   - **Input**: `src/pages/SettingsPage.tsx`
   - **Output**: Second tab lists agent configs from `GET /api/agent-configs/`. Each agent shown as expandable card: header with id, name, model, temperature, version, is_active badge. Expanded: textarea for system_prompt (monospace font, min-height ~300px), editable fields for model_identifier, temperature, is_active. Save button calls `PATCH /api/agent-configs/{id}`
   - **Test**: Agent configs load, expand/collapse works, edit + save works

5. [ ] Wire Settings page into router and sidebar
   - **Input**: `src/App.tsx`, `src/components/layout/Sidebar.tsx`
   - **Output**: Route `/settings` → `SettingsPage`. Sidebar nav item "Settings" with Settings icon, placed after "LLM Tracing"
   - **Test**: Navigation works, sidebar highlights correctly

6. [ ] Integration test — full edit flow
   - **Input**: All files from steps 1-5
   - **Output**: Manual verification: navigate to /settings, see grouped configs, edit a numeric value, save, refresh — value persists. Switch to Agent Configs tab, expand an agent, edit prompt text, save, refresh — prompt persists with incremented version
   - **Test**: End-to-end browser walkthrough confirming both tabs work

