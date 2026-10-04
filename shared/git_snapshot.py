"""Read untrusted object bytes without ever executing Git in their checkout.

Released layout: SHA-1, a direct checkout with a real .git directory, loose
objects or complete pack/index pairs, direct loose/packed refs. No alternates,
shallow history, linked worktrees, grafts or replacement refs. Limits bound the
input parser/copy, not a retry or production policy.
"""

from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat
import subprocess
from tempfile import TemporaryDirectory
import time

from shared.contracts.dto.commit_publication import PublicationFailure

MAX_BYTES = 1024 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_REF_BYTES = 1024 * 1024
MAX_DEPTH = 16
COPY_SECONDS = 60
_SHA = re.compile(r"[0-9a-f]{40}")
_PACK = re.compile(r"pack-[0-9a-f]{40}\.(pack|idx|rev)")
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class SnapshotRefusal(Exception):
    def __init__(self, failure, detail):
        self.failure = failure
        self.detail = detail
        super().__init__(detail)


def sterile_git_env(home: Path) -> dict[str, str]:
    """No inherited Git, credential, proxy, loader or platform-secret context."""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_ALLOW_PROTOCOL": "https",
    }


def _stamp(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


@contextmanager
def _directory(path):
    # Walk from / using directory descriptors; resolve() would follow symlinks.
    absolute = Path(os.path.abspath(path))
    fd = os.open("/", _DIRECTORY)
    try:
        for part in absolute.parts[1:]:
            new = os.open(part, _DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = new
        yield fd
    finally:
        os.close(fd)


class _ObjectReader:
    def __init__(self, destination):
        self.destination = destination
        self.manifest, self.ref_files = {}, {}
        self.total, self.entries = 0, 0
        self.deadline = time.monotonic() + COPY_SECONDS

    def visit(self, fd, relative, *, objects=False, depth=0):
        if depth > MAX_DEPTH:
            raise SnapshotRefusal(
                PublicationFailure.INSPECTION_FAILED, "Source ref depth exceeds snapshot bound"
            )
        names = sorted(os.listdir(fd))
        self.manifest[relative] = (_stamp(os.fstat(fd)), tuple(names))
        for name in names:
            self.entries += 1
            if self.entries > MAX_ENTRIES or time.monotonic() > self.deadline:
                raise SnapshotRefusal(
                    PublicationFailure.INSPECTION_FAILED,
                    "Source exceeds bounded snapshot; preserve and restore supported layout",
                )
            child = f"{relative}/{name}"
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                if objects:
                    valid = relative == "objects" and (
                        name in {"pack", "info"} or re.fullmatch(r"[0-9a-f]{2}", name)
                    )
                else:
                    valid = relative != "refs" or name in {"heads", "remotes", "tags"}
                if not valid:
                    raise SnapshotRefusal(
                        PublicationFailure.INSPECTION_FAILED,
                        "Unsupported object/ref directory or replacement refs",
                    )
                sub = os.open(name, _DIRECTORY, dir_fd=fd)
                try:
                    self.visit(sub, child, objects=objects, depth=depth + 1)
                finally:
                    os.close(sub)
                continue
            self.read_file(fd, relative, name, info, objects)
        if (_stamp(os.fstat(fd)), tuple(sorted(os.listdir(fd)))) != self.manifest[relative]:
            raise SnapshotRefusal(
                PublicationFailure.HEAD_CHANGED, "Source directory changed during snapshot"
            )

    def read_file(self, fd, relative, name, info, objects):
        limit = MAX_FILE_BYTES if objects else MAX_REF_BYTES
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise SnapshotRefusal(
                PublicationFailure.INSPECTION_FAILED,
                "Source contains indirection/nonregular or oversized data",
            )
        if objects:
            valid = (
                (relative == "objects/pack" and _PACK.fullmatch(name))
                or (
                    re.fullmatch(r"objects/[0-9a-f]{2}", relative)
                    and re.fullmatch(r"[0-9a-f]{38}", name)
                )
                or (relative == "objects/info" and name in {"packs", "commit-graph"})
            )
            if not valid:
                raise SnapshotRefusal(
                    PublicationFailure.INSPECTION_FAILED,
                    "Unsupported object indirection/promisor; restore complete ordinary objects",
                )
        self.total += info.st_size
        if self.total > MAX_BYTES:
            raise SnapshotRefusal(
                PublicationFailure.INSPECTION_FAILED, "Object snapshot exceeds 1 GiB bound"
            )
        opened = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(opened, "rb") as source:
            before = os.fstat(source.fileno())
            if _stamp(before) != _stamp(info):
                raise SnapshotRefusal(
                    PublicationFailure.HEAD_CHANGED, "Source changed during snapshot"
                )
            if not objects:
                self.ref_files[f"{relative}/{name}"] = source.read(limit + 1)
            elif self.destination is not None and relative != "objects/info":
                target = self.destination / f"{relative}/{name}"
                target.parent.mkdir(parents=True, exist_ok=True)
                copied = 0
                with target.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        copied += len(chunk)
                        if copied > limit or time.monotonic() > self.deadline:
                            raise SnapshotRefusal(
                                PublicationFailure.INSPECTION_FAILED,
                                "Object copy exceeded bound",
                            )
                        output.write(chunk)
            if _stamp(os.fstat(source.fileno())) != _stamp(before):
                raise SnapshotRefusal(
                    PublicationFailure.HEAD_CHANGED, "Source changed during object copy"
                )
            self.manifest[f"{relative}/{name}"] = _stamp(before)


class _Source:
    def __init__(self, path):
        self.path = path
        self.manifest = None
        self.refs = None

    def capture(self, destination=None):
        reader = _ObjectReader(destination)
        manifest, ref_files = reader.manifest, reader.ref_files

        with _directory(self.path) as workspace:
            manifest["workspace"] = _stamp(os.fstat(workspace))
            gitfd = os.open(".git", _DIRECTORY, dir_fd=workspace)
            try:
                manifest["git"] = _stamp(os.fstat(gitfd))
                names = os.listdir(gitfd)
                if any(name in names for name in ("commondir", "shallow")):
                    raise SnapshotRefusal(
                        PublicationFailure.INSPECTION_FAILED,
                        "Linked/shallow checkout unsupported; restore full direct checkout",
                    )
                for name in ("HEAD", "packed-refs"):
                    if name not in names:
                        if name == "HEAD":
                            raise SnapshotRefusal(
                                PublicationFailure.BRANCH_MISSING, "Source HEAD missing"
                            )
                        continue
                    opened = os.open(
                        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=gitfd
                    )
                    with os.fdopen(opened, "rb") as source:
                        before = os.fstat(source.fileno())
                        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_REF_BYTES:
                            raise SnapshotRefusal(
                                PublicationFailure.INSPECTION_FAILED, "Unsupported source ref file"
                            )
                        ref_files[name] = source.read(MAX_REF_BYTES + 1)
                        if _stamp(os.fstat(source.fileno())) != _stamp(before):
                            raise SnapshotRefusal(
                                PublicationFailure.HEAD_CHANGED, "Source ref changed during read"
                            )
                        manifest[name] = _stamp(before)
                if "info" in names:
                    infofd = os.open("info", _DIRECTORY, dir_fd=gitfd)
                    try:
                        if "grafts" in os.listdir(infofd):
                            raise SnapshotRefusal(
                                PublicationFailure.INSPECTION_FAILED, "Grafted history unsupported"
                            )
                    finally:
                        os.close(infofd)
                for name in ("objects", "refs"):
                    sub = os.open(name, _DIRECTORY, dir_fd=gitfd)
                    try:
                        reader.visit(sub, name, objects=name == "objects")
                    finally:
                        os.close(sub)
            finally:
                os.close(gitfd)
        packs = {key for key in manifest if key.startswith("objects/pack/")}
        for key in packs:
            prefix = key.rsplit(".", 1)[0]
            if f"{prefix}.pack" not in packs or f"{prefix}.idx" not in packs:
                raise SnapshotRefusal(
                    PublicationFailure.OBJECT_MISSING,
                    "Incomplete pack/index pair; restore preserved objects",
                )
        return manifest, ref_files

    def check(self):
        try:
            current = self.capture()
        except (OSError, ValueError, KeyError, UnicodeError) as exc:
            raise SnapshotRefusal(
                PublicationFailure.HEAD_CHANGED,
                "Source layout changed; preserve and reconcile the original checkout",
            ) from exc
        if current != (self.manifest, self.refs):
            raise SnapshotRefusal(
                PublicationFailure.HEAD_CHANGED,
                "Source identity/refs/objects changed; reconcile preserved checkout",
            )


def _refs(files):
    refs = {}
    remote_heads = {}
    for line in files.get("packed-refs", b"").decode("ascii").splitlines():
        if line.startswith("#") or line.startswith("^"):
            continue
        sha, ref = line.split(" ")
        if not _SHA.fullmatch(sha) or not ref.startswith(
            ("refs/heads/", "refs/remotes/", "refs/tags/")
        ):
            raise SnapshotRefusal(
                PublicationFailure.INSPECTION_FAILED, "Unsupported packed reference"
            )
        refs[ref] = sha
    for ref, value in files.items():
        if ref.startswith("refs/"):
            sha = value.decode("ascii").strip()
            if (
                ref.startswith("refs/remotes/")
                and ref.endswith("/HEAD")
                and sha.startswith("ref: ")
            ):
                target = sha.removeprefix("ref: ")
                if target.rsplit("/", 1)[0] != ref.rsplit("/", 1)[0]:
                    raise SnapshotRefusal(
                        PublicationFailure.INSPECTION_FAILED, "Foreign remote HEAD indirection"
                    )
                remote_heads[ref] = target
                continue
            if not _SHA.fullmatch(sha):
                raise SnapshotRefusal(
                    PublicationFailure.INSPECTION_FAILED, "Indirect or malformed source reference"
                )
            refs[ref] = sha
    for ref, target in remote_heads.items():
        if target not in refs:
            raise SnapshotRefusal(PublicationFailure.OBJECT_MISSING, "Remote HEAD target missing")
        refs[ref] = refs[target]
    return refs


@contextmanager
def object_snapshot(workspace: Path, *, branch=None, commit=None, repository_url=None):
    """Copy source bytes, prove a stable ref context, then expose only clean Git."""
    try:
        with TemporaryDirectory(prefix="commit-recovery-") as temporary:
            clean = Path(temporary) / "repository"
            gitdir = clean / ".git"
            (gitdir / "objects").mkdir(parents=True)
            source = _Source(workspace)
            source.manifest, source.refs = source.capture(gitdir)
            source.check()
            refs = _refs(source.refs)
            head = source.refs["HEAD"].decode("ascii").strip()
            if not head.startswith("ref: refs/heads/"):
                raise SnapshotRefusal(
                    PublicationFailure.WRONG_BRANCH, "Source HEAD must name its direct story branch"
                )
            head_ref = head.removeprefix("ref: ")
            if branch is not None and head_ref != f"refs/heads/{branch}":
                raise SnapshotRefusal(
                    PublicationFailure.WRONG_BRANCH,
                    "Source HEAD does not own the original story branch",
                )
            if head_ref not in refs:
                raise SnapshotRefusal(
                    PublicationFailure.OBJECT_MISSING, "Source branch reference missing"
                )
            if commit is not None and refs[head_ref] != commit:
                raise SnapshotRefusal(
                    PublicationFailure.HEAD_CHANGED, "Source HEAD differs from exact original SHA"
                )
            (gitdir / "HEAD").write_text(f"ref: {head_ref}\n")
            # Only the proven branch is needed for publication; GC additionally
            # needs local and remote refs to prove no unpublished work remains.
            selected = {head_ref: refs[head_ref]} if branch is not None else refs
            for ref, sha in selected.items():
                parts = ref.split("/")
                if any(part in {"", ".", ".."} for part in parts):
                    raise SnapshotRefusal(
                        PublicationFailure.INSPECTION_FAILED, "Source ref escapes snapshot"
                    )
                target = gitdir.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f"{sha}\n")
            config = "[core]\nrepositoryformatversion = 0\nbare = false\nhooksPath = /dev/null\n"
            if repository_url is not None:
                config += f'[remote "origin"]\nurl = {repository_url}\n'
            (gitdir / "config").write_text(config)
            env = sterile_git_env(Path(temporary))
            checked = subprocess.run(  # noqa: S603
                ["/usr/bin/git", "fsck", "--strict", "--no-reflogs", "--no-dangling"],
                cwd=clean,
                env=env,
                capture_output=True,
                timeout=60,
            )
            if checked.returncode:
                raise SnapshotRefusal(
                    PublicationFailure.OBJECT_MISSING,
                    "Corrupt/incomplete object snapshot; restore original objects before recovery",
                )
            yield clean, env, source
    except SnapshotRefusal:
        raise
    except (OSError, ValueError, KeyError, UnicodeError) as exc:
        raise SnapshotRefusal(
            PublicationFailure.INSPECTION_FAILED,
            "Unsupported/missing layout; preserve checkout and restore full direct objects",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SnapshotRefusal(
            PublicationFailure.TIMEOUT, "Bounded clean object inspection timed out"
        ) from exc
