"""Regression snapshot of the mock API: every dashboard route at a set of scene seconds, frozen clock.

  python3 demo/snapshot_api.py 1 > /tmp/scene1.json      # scene 1
  python3 demo/snapshot_api.py 2 > /tmp/scene2.json      # scene 2 (after the scene switch exists)

Compare two runs with `cmp`: the same scene must give byte-identical output after a refactor.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mock_api as M  # noqa: E402

FIXED = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
M.now = lambda: FIXED
scene = sys.argv[1] if len(sys.argv) > 1 else "1"
if hasattr(M, "set_scene"):
    M.set_scene(scene)

secs = [None, -5, 0, 3, 8, 15, 20, 25, 30, 35, 40, 45, 49, 52, 60, 80, 105, 110, 120, 140, 150, 160, 180, 190, 200,
        220, 240, 245, 250, 255, 260, 285, 290, 300, 340, 370, 385, 386, 400, 450, 470, 482, 488, 495, 520, 548, 553,
        570, 596, 597, 598, 600, 700]


def routes():
    tl = M.TL
    sid, pid = tl["story"]["id"], tl["project"]["id"]
    rid = tl["repository"]["id"]
    out = ["/api/admin/v2/attention", "/api/admin/v2/journeys", f"/api/admin/v2/journeys/{sid}",
           f"/api/admin/v2/journeys/{sid}/live", "/api/admin/v2/topology", "/api/admin/overview", "/api/projects",
           f"/api/projects/{pid}", f"/api/projects/{pid}/config/secrets/keys", "/api/stories", f"/api/stories/{sid}",
           "/api/tasks", "/api/repositories", "/api/applications", "/api/users", "/wm-api/workers",
           f"/wm-api/workspaces/{rid}"]
    for t in tl["tasks"]:
        out += [f"/api/tasks/{t['id']}", f"/api/tasks/{t['id']}/events",
                f"/api/admin/v2/journeys/{sid}/live?task={t['id']}"]
    for w in tl["workers"]:
        out += [f"/wm-api/workers/{w['id']}", f"/wm-api/workers/{w['id']}/logs?tail=200"]
    for bg in M.BACKGROUND:
        out += [f"/api/admin/v2/journeys/{bg['story_id']}"]
    return out


res = {}
for s in secs:
    if s is None:
        M.CLOCK.scene = lambda: None
    else:
        M.CLOCK.scene = (lambda x: (lambda: x))(float(s))
    for r in routes():
        path, _, qs = r.partition("?")
        from urllib.parse import parse_qs
        res[f"{s} {r}"] = M.route("GET", path, parse_qs(qs))
print(json.dumps(res, sort_keys=True, indent=0, default=str))
