#!/usr/bin/env python3
"""Brimos dashboard demo: serves the built admin-frontend plus a fake orchestrator API.

No production backend. The API answers the routes the dashboard calls, following the
contract in services/admin-frontend/src/types/api.ts, and the data moves along the scene
timeline (scene1.timeline.json).

Control:
  POST /demo/start?speed=1&at=0   start the scene (speed: scene seconds per real second, at: start offset)
  POST /demo/reset                back to idle: only the background products are visible
  POST /demo/seek?at=300          jump to a scene second (keeps speed)
  GET  /demo/state                current scene second, speed, bot stage

Build the frontend in demo mode (hides Queues/Servers/Logs/Settings in the sidebar):
  cd services/admin-frontend && VITE_DEMO=1 npm run build

Timestamps are written as "now minus (scene_now - t)", so durations on screen read in scene
seconds even when the scene runs accelerated.
"""
from __future__ import annotations

import json
import mimetypes
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
DIST = Path(os.environ.get("DEMO_DIST", HERE.parent / "services/admin-frontend/dist"))
TIMELINE = Path(os.environ.get("DEMO_TIMELINE", HERE / "scene1.timeline.json"))
HOST = os.environ.get("DEMO_HOST", "127.0.0.1")
PORT = int(os.environ.get("DEMO_PORT", "8790"))

TL = json.loads(TIMELINE.read_text())
STAGES = ["brief", "plan", "install", "build", "review", "deploy", "verify", "live"]


# ---------------------------------------------------------------- scene clock
class Clock:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.speed = 1.0
        self.offset = 0.0
        self.started = 0.0

    def start(self, speed: float, at: float) -> None:
        with self.lock:
            self.running, self.speed, self.offset, self.started = True, speed, at, time.time()

    def reset(self) -> None:
        with self.lock:
            self.running = False

    def scene(self) -> float | None:
        with self.lock:
            if not self.running:
                return None
            s = self.offset + (time.time() - self.started) * self.speed
            return min(s, TL["duration"] + 3600.0)


CLOCK = Clock()


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def now() -> datetime:
    return datetime.now(timezone.utc)


class View:
    """Snapshot of the scene at one moment."""

    def __init__(self) -> None:
        self.s = CLOCK.scene()
        self.now = now()

    @property
    def active(self) -> bool:
        return self.s is not None

    def ts(self, t: float | None) -> str | None:
        if t is None or self.s is None:
            return None
        return iso(self.now - timedelta(seconds=self.s - t))

    def at(self, series, default=None):
        """Step-function value of [[t, v], ...] at scene time."""
        if self.s is None:
            return default
        val = default
        for t, v in series:
            if self.s >= t:
                val = v
        return val

    def since(self, series) -> float | None:
        """Scene time when the current value of a step series began."""
        if self.s is None:
            return None
        start = None
        for t, _ in series:
            if self.s >= t:
                start = t
        return start

    def seen(self, t: float) -> bool:
        return self.s is not None and self.s >= t


# ---------------------------------------------------------------- background world
DAY = 86400
BG_USER = {"id": 7, "telegram_id": 600000007, "username": "maria_k", "first_name": "Maria", "last_name": None}
BACKGROUND = [
    {"project_id": "1d0e6a52-3c4b-4f1a-8e2d-a1b2c3d4e501", "title": "Habit Tracker", "slug": "habit-tracker",
     "story_id": "story-1f0b22c4", "story": "Habit tracker with weekly report", "ago": 5 * DAY, "took": 41 * 60,
     "server": "edge-eu-1", "port": 8011, "packages": [], "app_id": 101},
    {"project_id": "1d0e6a52-3c4b-4f1a-8e2d-a1b2c3d4e502", "title": "Expense Splitter", "slug": "expense-splitter",
     "story_id": "story-2a7c91e0", "story": "Split group expenses in a chat", "ago": 3 * DAY, "took": 58 * 60,
     "server": "edge-eu-1", "port": 8012, "packages": [], "app_id": 102},
    {"project_id": "1d0e6a52-3c4b-4f1a-8e2d-a1b2c3d4e503", "title": "Morning Tech Digest", "slug": "morning-tech-digest",
     "story_id": "story-411b0118", "story": "Morning digest of new posts from Telegram channels", "ago": 1 * DAY, "took": 33 * 60,
     "server": "edge-eu-2", "port": 8021, "packages": [{"name": "tg-channels", "version": "0.1.2", "kind": "package"}],
     "platform": ["tg-reader"], "app_id": 103},
    {"project_id": "1d0e6a52-3c4b-4f1a-8e2d-a1b2c3d4e504", "title": "Pet Vaccination Reminders", "slug": "pet-vaccination-reminders",
     "story_id": "story-5e3d8a17", "story": "Remind me about my dog's vaccinations", "ago": 9 * DAY, "took": 47 * 60,
     "server": "edge-eu-2", "port": 8022, "packages": [], "app_id": 104},
]
SERVERS = [
    {"handle": "control-1", "role": "control", "ram": 16384, "used": 9100, "cpu": 23.0, "ip": "10.0.0.10"},
    {"handle": "edge-eu-1", "role": "product", "ram": 8192, "used": 2304, "cpu": 11.0, "ip": "10.0.1.21"},
    {"handle": "edge-eu-2", "role": "product", "ram": 8192, "used": 1792, "cpu": 9.0, "ip": "10.0.1.22"},
]


def bg_times(bg, n: datetime):
    created = n - timedelta(seconds=bg["ago"])
    return created, created + timedelta(seconds=bg["took"])


def bg_container(bg):
    return {"name": bg["slug"], "status": "running", "placement": bg["server"], "port": bg["port"],
            "reserved_ram_mb": 256, "response_time_ms": 70 + bg["port"] % 40, "uptime_pct_24h": 100.0,
            "deployed_sha": f"{abs(hash(bg['slug'])) % (1 << 40):010x}"}


def bg_passport(bg):
    return {"modules": [p["name"] for p in bg["packages"]], "packages": bg["packages"],
            "containers": [bg_container(bg)], "user_secrets": ["TELEGRAM_BOT_TOKEN"]}


def bg_summary(bg, n):
    created, finished = bg_times(bg, n)
    return {"story_id": bg["story_id"], "title": bg["story"], "project_id": bg["project_id"],
            "project_title": bg["title"], "status": "completed", "waiting_on": "none", "current_stage": "live",
            "created_at": iso(created), "updated_at": iso(finished), "finished_at": iso(finished)}


def bg_journey(bg, n):
    created, finished = bg_times(bg, n)
    span = (finished - created).total_seconds()
    cuts = [0, 0.04, 0.12, 0.13, 0.62, 0.8, 0.9, 0.99, 1.0]
    steps = []
    for i, stage in enumerate(STAGES):
        a = created + timedelta(seconds=span * cuts[i])
        b = created + timedelta(seconds=span * cuts[i + 1])
        skipped = stage == "install" and not bg["packages"]
        steps.append({"stage": stage, "status": "skipped" if skipped else "done",
                      "started_at": None if skipped else iso(a), "finished_at": None if skipped else iso(b),
                      "attempts": [], "facts": []})
    return {"summary": bg_summary(bg, n), "request": bg["story"], "requirements": 6, "pr_number": 1,
            "steps": steps, "passport": bg_passport(bg), "links": []}


# ---------------------------------------------------------------- scene entities
P = TL["project"]
ST = TL["story"]
REPO = TL["repository"]
APP = TL["application"]
U = TL["user"]


def scene_user(v: View):
    return {**U, "is_admin": False, "created_at": v.ts(-240), "updated_at": None, "last_seen": iso(v.now)}


def scene_project(v: View):
    return {"id": P["id"], "title": P["title"], "slug": P["slug"], "status": v.at(P["status"], "scaffolding"),
            "config": {}, "owner_id": U["id"], "project_spec": None, "created_at": v.ts(0), "updated_at": iso(v.now)}


def scene_story(v: View):
    status = v.at(ST["status"], "created")
    return {"id": ST["id"], "project_id": P["id"], "title": ST["title"], "description": ST["description"],
            "acceptance_criteria": ST["acceptance_criteria"] if v.seen(20) else None, "type": "product",
            "status": status, "waiting_on": v.at(ST["waiting_on"], "none"), "priority": 1,
            "created_by": "product_owner", "pr_number": v.at(ST["pr_number"]),
            "status_entered_at": v.ts(v.since(ST["status"])), "created_at": v.ts(0), "updated_at": iso(v.now)}


def task_status(v: View, task) -> str | None:
    return v.at(task["status"])


def scene_tasks(v: View):
    out = []
    for i, t in enumerate(TL["tasks"]):
        st = task_status(v, t)
        if st is None:
            continue
        out.append({"id": t["id"], "project_id": P["id"], "story_id": ST["id"], "type": t["type"], "title": t["title"],
                    "description": t["title"], "plan": None, "status": st, "priority": i + 1,
                    "acceptance_criteria": None, "current_iteration": 1, "max_iterations": 5, "created_by": "architect",
                    "repository_id": REPO["id"], "blocked_by_task_id": None, "failure_metadata": None,
                    "dispatch_admitted": st not in ("backlog",), "created_at": v.ts(t["status"][0][0]),
                    "updated_at": v.ts(v.since(t["status"])), "last_event": f"status → {st}",
                    "elapsed_minutes": round((v.s - t["status"][0][0]) / 60, 1)})
    return out


def task_events(v: View, task_id: str):
    for t in TL["tasks"]:
        if t["id"] != task_id:
            continue
        ev, prev = [], None
        for n, (at, st) in enumerate(t["status"]):
            if not v.seen(at):
                break
            ev.append({"id": n + 1, "task_id": task_id, "event_type": "created" if prev is None else "status_changed",
                       "from_status": prev, "to_status": st, "iteration": 1, "details": {},
                       "actor": "architect" if prev is None else ("coding worker" if st != "done" else "Brimos"),
                       "created_at": v.ts(at)})
            prev = st
        return ev
    return None


def attempt_view(v: View, a):
    if not v.seen(a["start"]):
        return None
    done = v.seen(a["end"])
    status = (a.get("end_status", "completed") if done else "running")
    return {"id": a["id"], "kind": a["kind"], "status": status,
            "task_id": TL["tasks"][a["task"] - 1]["id"] if a.get("task") else None, "actor": a["actor"],
            "started_at": v.ts(a["start"]), "finished_at": v.ts(a["end"]) if done else None,
            "error": a.get("error") if done else None}


def scene_steps(v: View):
    steps = []
    for stage in STAGES:
        spec = TL["steps"][stage]
        status = v.at(spec["status"], "pending")
        first = spec["status"][0][0]
        done_at = next((t for t, s in spec["status"] if s in ("done",)), None)
        started = v.ts(first) if status != "pending" and status != "skipped" else None
        finished = v.ts(done_at) if status == "done" and done_at is not None else None
        attempts = [x for x in (attempt_view(v, a) for a in TL["attempts"] if a["stage"] == stage) if x]
        facts = [{"label": f[1], "value": f[2]} for f in spec.get("facts", []) if v.seen(f[0])]
        steps.append({"stage": stage, "status": status, "started_at": started, "finished_at": finished,
                      "attempts": attempts, "facts": facts})
    return steps


def current_stage(steps):
    cur = None
    for s in steps:
        if s["status"] in ("active", "waiting", "failed"):
            return s["stage"]
        if s["status"] == "done":
            cur = s["stage"]
    return cur


def scene_summary(v: View, steps=None):
    steps = steps or scene_steps(v)
    story = scene_story(v)
    done = story["status"] == "completed"
    return {"story_id": ST["id"], "title": ST["title"], "project_id": P["id"], "project_title": P["title"],
            "status": story["status"], "waiting_on": story["waiting_on"], "current_stage": current_stage(steps),
            "created_at": v.ts(0), "updated_at": iso(v.now), "finished_at": v.ts(598) if done else None}


def scene_app_status(v: View):
    return v.at(APP["status"])


def scene_passport(v: View):
    containers = []
    st = scene_app_status(v)
    if st:
        containers.append({"name": APP["service_name"], "status": st, "placement": APP["server"], "port": APP["port"],
                           "reserved_ram_mb": 256, "response_time_ms": 84 if st == "running" else None,
                           "uptime_pct_24h": 100.0 if st == "running" else None,
                           "deployed_sha": APP["sha"] if st == "running" else None})
    return {"modules": [], "packages": [], "containers": containers,
            "user_secrets": ["TELEGRAM_BOT_TOKEN"] if v.seen(490) else []}


def scene_journey(v: View):
    steps = scene_steps(v)
    return {"summary": scene_summary(v, steps), "request": ST["request"], "requirements": v.at(ST["requirements"]),
            "pr_number": v.at(ST["pr_number"]), "steps": steps, "passport": scene_passport(v), "links": []}


def scene_workers(v: View):
    out = []
    for w in TL["workers"]:
        if not v.seen(w["from"]) or v.seen(w["to"]):
            continue
        task = next((t for t in TL["tasks"] if t.get("worker") == w["id"] and task_status(v, t) in ("in_dev", "in_ci")), None)
        busy = task is not None or w.get("qa")
        out.append({"id": w["id"], "status": "RUNNING", "project_id": P["id"], "repo_id": REPO["id"],
                    "workspace_path": f"/workspaces/{P['slug']}", "dev_network": None, "error": None,
                    "container": {"id": w["id"][4:], "image": "brimos/worker:2026.10", "state": "running"},
                    "container_error": None, "agent_process_status": "working" if busy else "idle",
                    "agent_process_status_error": None,
                    "active_turn_lease": {"attempt_id": "att", "request_id": f"req-{(task or {}).get('id', w['id'])[-12:]}",
                                          "lease_id": "l", "started_at": iso(v.now), "deadline_at": iso(v.now)} if busy else None,
                    "active_turn_lease_error": None, "story_bindings": [ST["id"]], "story_bindings_error": None,
                    "attempt_run": {"id": f"run-{w['id'][4:]}", "status": "running"} if busy else None,
                    "attempt_run_error": None, "waiting_attempt": None, "waiting_attempt_error": None,
                    "container_id": w["id"][4:], "image": "brimos/worker:2026.10"})
    return out


# ---------------------------------------------------------------- endpoint assembly
def all_projects(v: View):
    n = v.now
    out = []
    for bg in BACKGROUND:
        created, finished = bg_times(bg, n)
        out.append({"id": bg["project_id"], "title": bg["title"], "slug": bg["slug"], "status": "active", "config": {},
                    "owner_id": BG_USER["id"], "created_at": iso(created), "updated_at": iso(finished)})
    if v.active:
        out.insert(0, scene_project(v))
    return out


def bg_story(bg, n):
    s = bg_summary(bg, n)
    return {"id": bg["story_id"], "project_id": bg["project_id"], "title": bg["story"], "description": bg["story"],
            "type": "product", "status": "completed", "waiting_on": "none", "priority": 1, "created_by": "product_owner",
            "pr_number": 1, "created_at": s["created_at"], "updated_at": s["updated_at"]}


def attention(v: View):
    steps = scene_steps(v) if v.active else []
    building = v.active and scene_story(v)["status"] not in ("completed",)
    items = []
    if v.active:
        for a in TL["attention"]:
            if v.seen(a["from"]) and not v.seen(a["to"]):
                items.append({**a["item"], "since": v.ts(a["from"]), "project_id": P["id"], "project_title": P["title"],
                              "story_id": ST["id"], "task_id": None, "application_id": None, "server_handle": None})
    running = len(scene_workers(v)) if v.active else 0
    queued = sum(1 for t in scene_tasks(v) if t["status"] == "todo") if v.active else 0
    live = len(BACKGROUND) + (1 if v.active and v.seen(597) else 0)
    del steps
    return {"kpis": {"active_journeys": 1 if building else 0, "running_runs": running, "queued_runs": queued,
                     "live_products": live, "degraded_containers": 0, "median_lead_time_minutes_7d": 44.0},
            "items": items}


def topology(v: View):
    n = v.now
    placements = [{"handle": s["handle"], "role": s["role"], "status": "in_use", "public_ip": s["ip"],
                   "capacity_cpu": 4, "capacity_ram_mb": s["ram"],
                   "used_ram_mb": s["used"] + (256 if s["handle"] == APP["server"] and v.active and scene_app_status(v) else 0),
                   "cpu_usage_pct": s["cpu"], "last_health_check": iso(n)} for s in SERVERS]
    products = [{"project_id": bg["project_id"], "title": bg["title"], "slug": bg["slug"], "status": "active",
                 "latest_story_id": bg["story_id"], "passport": bg_passport(bg)} for bg in BACKGROUND]
    if v.active:
        products.append({"project_id": P["id"], "title": P["title"], "slug": P["slug"],
                         "status": v.at(P["status"], "scaffolding"), "latest_story_id": ST["id"],
                         "passport": scene_passport(v)})
    services = [{"name": "tg-reader", "status": "up",
                 "products": [bg["project_id"] for bg in BACKGROUND if "tg-reader" in bg.get("platform", [])]}]
    return {"placements": placements, "products": products, "platform_services": services}


def application(v: View):
    st = scene_app_status(v)
    if not st:
        return None
    return {"id": APP["id"], "repo_id": REPO["id"], "server_handle": APP["server"], "service_name": APP["service_name"],
            "reserved_ram_mb": 256, "ports": [{"id": 1, "server_handle": APP["server"], "port": APP["port"],
                                              "service_name": APP["service_name"], "application_id": APP["id"],
                                              "created_at": v.ts(495)}],
            "status": st, "last_health_check": iso(v.now) if st == "running" else None,
            "response_time_ms": 84 if st == "running" else None, "uptime_pct_24h": 100.0 if st == "running" else None,
            "monitoring_enabled": True, "created_at": v.ts(495), "updated_at": iso(v.now)}


def repository(v: View):
    if not v.seen(REPO["from"]):
        return None
    return {"id": REPO["id"], "project_id": P["id"], "name": REPO["name"], "git_url": REPO["git_url"],
            "provider_repo_id": 90210, "role": "primary", "visibility": "private", "is_managed": True,
            "bot_username": "brimos_demo_first_part_bot", "created_at": v.ts(REPO["from"])}


def route(method: str, path: str, q: dict):
    """Return (status, body). body None → 404."""
    v = View()
    one = lambda k, d=None: (q.get(k) or [d])[0]  # noqa: E731

    if method != "GET":
        return 200, {}

    p = path.rstrip("/") or "/"
    # --- console v2
    if p == "/api/admin/v2/attention":
        return 200, attention(v)
    if p == "/api/admin/v2/journeys":
        rows = [bg_summary(bg, v.now) for bg in BACKGROUND]
        if v.active:
            rows.append(scene_summary(v))
        rows.sort(key=lambda r: r["updated_at"], reverse=True)
        return 200, rows
    if p.startswith("/api/admin/v2/journeys/"):
        sid = p.rsplit("/", 1)[1]
        if v.active and sid == ST["id"]:
            return 200, scene_journey(v)
        bg = next((b for b in BACKGROUND if b["story_id"] == sid), None)
        return (200, bg_journey(bg, v.now)) if bg else (404, None)
    if p == "/api/admin/v2/topology":
        return 200, topology(v)
    if p == "/api/admin/overview":
        tasks = scene_tasks(v) if v.active else []
        counts = {k: 0 for k in ["backlog", "todo", "in_dev", "in_ci", "testing", "done", "blocked",
                                 "waiting_human_review", "waiting_resources", "failed", "cancelled"]}
        for t in tasks:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        running = len(scene_workers(v)) if v.active else 0
        return 200, {"queues": {"status": "ok", "streams": [], "bindings": [], "issues": []}, "task_counts": counts,
                     "paid_runs": {"queued": counts["todo"], "running": running,
                                   "by_executor": {"claude": {"queued": counts["todo"], "running": running}} if running else {},
                                   "unavailable_executor_decisions": 0},
                     "recent_failed_runs": [], "waiting_stories": []}

    # --- reference entities
    if p == "/api/projects":
        owner = one("owner_id")
        rows = all_projects(v)
        if owner:
            rows = [r for r in rows if str(r["owner_id"]) == owner]
        return 200, rows
    if p.startswith("/api/projects/"):
        pid = p.split("/")[3]
        return next(((200, r) for r in all_projects(v) if r["id"] == pid), (404, None))
    if p == "/api/stories":
        pid = one("project_id")
        rows = [bg_story(bg, v.now) for bg in BACKGROUND]
        if v.active:
            rows.append(scene_story(v))
        return 200, [r for r in rows if not pid or r["project_id"] == pid]
    if p.startswith("/api/stories/"):
        sid = p.split("/")[3]
        if v.active and sid == ST["id"]:
            return 200, scene_story(v)
        bg = next((b for b in BACKGROUND if b["story_id"] == sid), None)
        return (200, bg_story(bg, v.now)) if bg else (404, None)
    if p == "/api/tasks":
        rows = scene_tasks(v) if v.active else []
        sid, pid = one("story_id"), one("project_id")
        return 200, [r for r in rows if (not sid or r["story_id"] == sid) and (not pid or r["project_id"] == pid)]
    if p.startswith("/api/tasks/"):
        parts = p.split("/")
        tid = parts[3]
        if len(parts) > 4 and parts[4] == "events":
            ev = task_events(v, tid) if v.active else None
            return (200, ev) if ev is not None else (404, None)
        return next(((200, t) for t in (scene_tasks(v) if v.active else []) if t["id"] == tid), (404, None))
    if p == "/api/repositories":
        r = repository(v) if v.active else None
        return 200, [r] if r and one("project_id") in (None, P["id"]) else []
    if p.startswith("/api/repositories/"):
        r = repository(v) if v.active else None
        return (200, r) if r else (404, None)
    if p == "/api/applications":
        a = application(v) if v.active else None
        return 200, [a] if a and one("repo_id") in (None, REPO["id"]) else []
    if p.startswith("/api/applications/"):
        parts = p.split("/")
        a = application(v) if v.active else None
        if len(parts) > 4:
            if parts[4] == "health-history":
                if not a:
                    return 200, []
                return 200, [{"id": i, "application_id": APP["id"], "recorded_at": v.ts(546 + i * 10),
                              "metrics": {"response_time_ms": 80 + i * 3, "status_code": 200, "healthy": True},
                              "created_at": v.ts(546 + i * 10), "updated_at": None}
                             for i in range(6) if v.seen(546 + i * 10)]
            if parts[4] == "runs":
                return 200, []
        return (200, a) if a else (404, None)
    if p == "/api/users":
        rows = [{**BG_USER, "is_admin": False, "created_at": iso(v.now - timedelta(days=20)), "last_seen": iso(v.now)}]
        if v.active:
            rows.insert(0, scene_user(v))
        return 200, rows
    if p.startswith("/api/users/"):
        uid = p.split("/")[3]
        if v.active and uid == str(U["id"]):
            return 200, scene_user(v)
        if uid == str(BG_USER["id"]):
            return 200, {**BG_USER, "is_admin": False, "created_at": iso(v.now - timedelta(days=20)), "last_seen": iso(v.now)}
        return 404, None
    if p == "/api/servers":
        return 200, [{"handle": s["handle"], "host": s["handle"], "public_ip": s["ip"], "status": "in_use",
                      "is_managed": True, "capacity_cpu": 4, "capacity_ram_mb": s["ram"], "used_ram_mb": s["used"],
                      "cpu_usage_pct": s["cpu"], "labels": {"role": s["role"]}, "created_at": iso(v.now - timedelta(days=60)),
                      "last_health_check": iso(v.now)} for s in SERVERS]
    if p.startswith("/api/servers/"):
        return 200, []
    if p == "/api/engineering-consumer/drain":
        return 200, {"draining": False, "requested_at": None, "actor": None}
    if p == "/api/debug/queues":
        return 200, {"status": "ok", "streams": [], "bindings": [], "issues": []}
    if p in ("/api/system-configs", "/api/agent-configs"):
        return 200, []

    # --- worker manager
    if p == "/wm-api/workers":
        return 200, scene_workers(v) if v.active else []
    if p.startswith("/wm-api/workers/"):
        parts = p.split("/")
        w = next((x for x in (scene_workers(v) if v.active else []) if x["id"] == parts[3]), None)
        if len(parts) > 4 and parts[4] == "logs":
            return 200, {"worker_id": parts[3], "logs": "", "tail": int(one("tail", "200"))}
        if len(parts) > 4 and parts[4] == "tree":
            return 200, []
        return (200, w) if w else (404, None)
    if p.startswith("/wm-api/workspaces/"):
        return 200, []
    return 404, None


# ---------------------------------------------------------------- HTTP
def bot_stage(s: float | None) -> str | None:
    if s is None:
        return None
    for st in TL["bot_stages"]:
        if s < st["to"]:
            return st["name"]
    return "Done"


class Handler(BaseHTTPRequestHandler):
    server_version = "brimos-demo"

    def log_message(self, fmt, *args):  # noqa: N802
        if os.environ.get("DEMO_LOG"):
            sys.stderr.write("%s\n" % (fmt % args))

    def _json(self, status: int, body) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _demo(self, method: str, path: str, q: dict) -> None:
        def num(k, d):
            try:
                return float((q.get(k) or [d])[0])
            except ValueError:
                return float(d)
        if path == "/demo/start" and method == "POST":
            CLOCK.start(num("speed", 1), num("at", 0))
        elif path == "/demo/seek" and method == "POST":
            CLOCK.start(CLOCK.speed if CLOCK.running else num("speed", 1), num("at", 0))
        elif path == "/demo/reset" and method == "POST":
            CLOCK.reset()
        elif path != "/demo/state":
            return self._json(404, {"detail": "unknown demo command"})
        s = CLOCK.scene()
        self._json(200, {"running": CLOCK.running, "scene_second": None if s is None else round(s, 1),
                         "speed": CLOCK.speed, "bot_stage": bot_stage(s), "story_id": ST["id"]})

    def _handle(self, method: str) -> None:
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.startswith("/demo/"):
            return self._demo(method, u.path, q)
        if u.path == "/health":
            return self._json(200, {"status": "ok"})
        if u.path.startswith("/api/") or u.path.startswith("/wm-api/"):
            status, body = route(method, u.path, q)
            if body is None:
                sys.stderr.write(f"mock 404: {method} {self.path}\n")
                return self._json(404, {"detail": "not in demo mock"})
            return self._json(status, body)
        return self._static(u.path)

    def _static(self, path: str) -> None:
        f = (DIST / path.lstrip("/")).resolve()
        if not str(f).startswith(str(DIST.resolve())) or not f.is_file():
            f = DIST / "index.html"
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(f.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        self._handle("GET")

    def do_POST(self):  # noqa: N802
        self._handle("POST")

    def do_PUT(self):  # noqa: N802
        self._handle("PUT")

    def do_PATCH(self):  # noqa: N802
        self._handle("PATCH")

    def do_DELETE(self):  # noqa: N802
        self._handle("DELETE")


if __name__ == "__main__":
    if not (DIST / "index.html").is_file():
        sys.exit(f"no built frontend at {DIST}; run VITE_DEMO=1 npm run build in services/admin-frontend")
    if os.environ.get("DEMO_AUTOSTART"):
        CLOCK.start(float(os.environ.get("DEMO_SPEED", "1")), 0)
    print(f"brimos dashboard demo on http://{HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
