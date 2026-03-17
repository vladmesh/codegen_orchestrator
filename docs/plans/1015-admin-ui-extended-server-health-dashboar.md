# #1015 Admin UI: extended server health dashboard with per-container view + charts

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Plan

### Step 1: Update TypeScript types
Add health fields to Server interface + new MetricsHistoryEntry, MetricsSnapshot, ContainerMetrics, Incident interfaces.

### Step 2: Add utility functions
formatBytes, formatUptime, freshnessColor in utils.ts.

### Step 3: Add CPU bar + health indicators to main table
CPU UsageBar in Resources column, last_health_check freshness in Updated column.

### Step 4: Tab infrastructure in expanded row
Replace single Applications section with tabs: Overview, Containers, Charts, Incidents.

### Step 5: Overview tab
Health summary cards (load avg, network errors, containers, uptime) + applications table.

### Step 6: Containers tab
Fetch latest metrics-history entry, render per-container table with CPU/RAM.

### Step 7: Charts tab (Recharts)
Time range selector (1h/24h), CPU/RAM/Disk area charts from metrics history.

### Step 8: Incidents tab
Fetch incidents, render table with type, status, detected/resolved timestamps.

### Step 9: StatusBadge incident colors
Add detected/recovering/resolved colors.

### Step 10: Extract to files if >400 lines
