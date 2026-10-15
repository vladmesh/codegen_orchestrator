"""Shared engine for a scene's live material: log lines, repository state and diffs.

A scene module (scene1_feed.py, scene2_feed.py) writes its SCRIPT of Lines and its FILES
(scene*_repo.py), then wraps them in a Feed. One script drives three views so they never disagree:
  - the live log (story-wide, per task, per worker);
  - the repository tree and file contents at any scene second;
  - the diff of a task or of the pull request against the scaffold.
Commit stats ("{stat:scaffold}", "{stat:<task>}") are computed from the real diffs.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass

ANSI = {"cmd": "\x1b[36m$ ", "ok": "\x1b[32m", "err": "\x1b[31m", "info": "\x1b[33m", "file": "\x1b[35m",
        "step": "\x1b[1m", "out": ""}
RECENT = 12  # scene seconds a file stays marked as just written


@dataclass
class Line:
    t: float
    who: str
    kind: str  # cmd | out | ok | err | info | file | step
    text: str
    task: int | None = None  # 1..n, task the line belongs to
    file: tuple[str, int] | None = None  # (path, version index) written at this moment
    n: int = 0


def task_script(task: int, who: str, start: float, end: float, steps: list[tuple]) -> list[Line]:
    """steps: (fraction of [start, end], kind, text[, (path, version)])."""
    out = []
    for step in steps:
        frac, kind, text = step[:3]
        f = step[3] if len(step) > 3 else None
        out.append(Line(round(start + (end - start) * frac, 1), who, kind, text, task, f))
    return out


def ci_task(task: int, branch: str, start: float, end: float, tests: str) -> list[Line]:
    span = end - start
    return [
        Line(start + 0.5, "ci", "step", f"CI · {branch}", task),
        Line(start + span * 0.3, "ci", "cmd", "ruff check .", task),
        Line(start + span * 0.4, "ci", "out", "All checks passed!", task),
        Line(start + span * 0.5, "ci", "cmd", "pytest -q", task),
        Line(start + span * 0.85, "ci", "ok", tests, task),
        Line(end, "brimos", "ok", f"Merged {branch} into main", task),
    ]


def _diff(old: str | None, new: str) -> tuple[list[str], int, int]:
    a = old.splitlines() if old is not None else []
    b = new.splitlines()
    lines = list(difflib.unified_diff(a, b, lineterm="", n=2))[2:]  # drop ---/+++ headers
    adds = sum(1 for x in lines if x.startswith("+"))
    dels = sum(1 for x in lines if x.startswith("-"))
    return lines, adds, dels


class Feed:
    def __init__(self, lines: list[Line], files: dict[str, list[str]], task_span: dict[int, tuple[float, float]],
                 scaffold_done: float, worker_of: dict[str, str], names: dict[str, str]) -> None:
        lines.sort(key=lambda x: x.t)
        for i, line in enumerate(lines):
            line.n = i + 1
        self.script = lines
        self.files = files
        self.task_span = task_span
        self.scaffold_done = scaffold_done
        self.worker_of = worker_of
        self.names = names
        for line in self.script:
            if line.text.startswith("{stat:"):
                line.text = self._stat(line.text[6:-1])

    # ------------------------------------------------------------ repository state
    def file_events(self) -> list[Line]:
        return [x for x in self.script if x.file]

    def repo_at(self, s: float, upto_task: int | None = None) -> dict[str, tuple[int, float, int | None]]:
        """path → (version, time written, task) for every file written by scene second s."""
        state: dict[str, tuple[int, float, int | None]] = {}
        for e in self.file_events():
            if e.t > s:
                break
            state[e.file[0]] = (e.file[1], e.t, e.task)
        return state

    def content(self, path: str, ver: int) -> str:
        return self.files[path][ver]

    def changes(self, s: float, base_t: float, task: int | None = None) -> list[dict]:
        """Files changed after base_t up to s (only by `task` if given), newest change first."""
        base = self.repo_at(base_t)
        head = {}
        for e in self.file_events():
            if e.t > s:
                break
            if e.t <= base_t or (task is not None and e.task != task):
                continue
            head[e.file[0]] = (e.file[1], e.t)
        out = []
        for path, (ver, t) in head.items():
            old = self.content(path, base[path][0]) if path in base else None
            lines, adds, dels = _diff(old, self.content(path, ver))
            out.append({"path": path, "status": "modified" if old is not None else "added",
                        "additions": adds, "deletions": dels, "patch": "\n".join(lines), "t": t})
        out.sort(key=lambda f: -f["t"])
        return out

    def tree(self, s: float) -> list[dict]:
        state = self.repo_at(s)
        dirs = set()
        for path in state:
            parts = path.split("/")
            for i in range(1, len(parts)):
                dirs.add("/".join(parts[:i]))
        rows = [{"path": d, "is_dir": True, "size": 0} for d in sorted(dirs)]
        rows += [{"path": p, "is_dir": False, "size": len(self.content(p, v).encode())}
                 for p, (v, _, _) in sorted(state.items())]
        return rows

    def _stat(self, key: str) -> str:
        if key == "scaffold":
            files = self.changes(self.scaffold_done, -1)
        else:
            n = int(key)
            files = self.changes(self.task_span[n][1], self.task_span[n][0] - 0.01, task=n)
        adds = sum(f["additions"] for f in files)
        dels = sum(f["deletions"] for f in files)
        plural = lambda n, w: f"{n} {w}{'s' if n != 1 else ''}"  # noqa: E731
        parts = [f"{plural(len(files), 'file')} changed", f"{plural(adds, 'insertion')}(+)"]
        if dels:
            parts.append(f"{plural(dels, 'deletion')}(-)")
        return ", ".join(parts)

    # ------------------------------------------------------------ log views
    def log(self, s: float, task: int | None = None, who: str | None = None, limit: int = 400) -> list[Line]:
        rows = [x for x in self.script if x.t <= s and (task is None or x.task == task) and (who is None or x.who == who)]
        return rows[-limit:]

    def worker_log_text(self, worker_id: str, s: float, tail: int) -> str:
        who = next((k for k, v in self.worker_of.items() if v == worker_id), None)
        if who is None:
            return ""
        rows = self.log(s, who=who, limit=tail)
        return "\n".join(f"{ANSI[x.kind]}{x.text}\x1b[0m" for x in rows)

    # ------------------------------------------------------------ dashboard payload
    def _last_write(self, s: float, path: str | None = None, task: int | None = None) -> Line | None:
        last = None
        for e in self.file_events():
            if e.t > s:
                break
            if (path is None or e.file[0] == path) and (task is None or e.task == task):
                last = e
        return last

    def _file_change(self, e: Line) -> dict:
        """Diff of one write against the previous version of the file."""
        path, ver = e.file
        old = self.content(path, ver - 1) if ver > 0 else None
        lines, adds, dels = _diff(old, self.content(path, ver))
        return {"path": path, "status": "modified" if old is not None else "added", "additions": adds,
                "deletions": dels, "patch": "\n".join(lines), "author": self.names[e.who], "t": e.t, "task": e.task}

    def live(self, s: float, ts, task: int | None = None, file: str | None = None, limit: int = 160) -> dict:
        """Everything the Live log / Repository panels need at scene second s. ts(t) → iso timestamp."""
        rows = self.log(s, task=task, limit=limit)
        lines = [{"n": x.n, "at": ts(x.t), "source": self.names[x.who], "kind": x.kind, "text": x.text} for x in rows]
        state = self.repo_at(s)
        base = self.repo_at(self.scaffold_done)
        files = []
        for row in self.tree(s):
            entry = dict(row)
            if not row["is_dir"]:
                ver, t, _ = state[row["path"]]
                if s - t <= RECENT and t > self.scaffold_done:
                    entry["mark"] = "edited" if ver > 0 else "new"
                # +/- against the scaffold, i.e. what the pull request adds
                if t > self.scaffold_done:
                    old = self.content(row["path"], base[row["path"]][0]) if row["path"] in base else None
                    _, entry["additions"], entry["deletions"] = _diff(old, self.content(row["path"], ver))
            files.append(entry)
        focus = None
        if file and file in state:
            e = self._last_write(s, path=file)
            focus = self._file_change(e) if e else None
        else:
            e = self._last_write(s, task=task)
            focus = self._file_change(e) if e and e.t > (self.scaffold_done if task is None else -1) else None
        if focus:
            focus["at"] = ts(focus.pop("t"))
            focus.pop("task", None)
        task_changes = None
        if task is not None:
            start, end = self.task_span[task]
            task_changes = [{k: v for k, v in c.items() if k != "t"}
                            for c in self.changes(min(s, end), start - 0.01, task=task)]
        return {"lines": lines, "files": files, "focus": focus, "task_changes": task_changes}
