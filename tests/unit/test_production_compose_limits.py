"""Production containers rotate their logs, and the sized ones stay inside a bound.

The host runs Docker's json-file driver, which keeps a container's stdout for
ever unless the container says otherwise. One chatty service is then enough to
fill the disk out from under every other service on the box, and the failure
arrives as "no space left on device" somewhere unrelated. The rotation therefore
has to be a property of the production overlay itself rather than of whoever
last edited a service, so this asserts two things: every service the overlay
defines is bounded, and the overlay defines every service the base file does —
without the second, a new base service ships to production unrotated and nothing
notices.

The compose files are parsed rather than rendered by `docker compose config`,
because that needs a Docker daemon and this suite runs without one. The overlay
uses compose's own `!reset` / `!override` tags, which are not YAML, so the loader
below reads them as the plain values they wrap.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
BASE = ROOT / "docker-compose.yml"
PROD = ROOT / "docker-compose.prod.yml"

#: Bytes one container's logs may occupy: max-size × max-file must not exceed it.
MAX_LOG_BYTES_PER_SERVICE = 512 * 1024 * 1024

#: Bytes the whole limited platform may reserve, leaving the production host room
#: for the 4 GiB coding worker `engineering.worker_slots=1` allows and for the
#: services deliberately left unsized.
MAX_TOTAL_MEM_BYTES = 12 * 1024**3

_UNITS = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


class _ComposeLoader(yaml.SafeLoader):
    """A safe loader that reads compose's merge tags as their wrapped value."""


def _passthrough_tag(loader, node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_scalar(node)


for _tag in ("!reset", "!override"):
    _ComposeLoader.add_constructor(_tag, _passthrough_tag)


def _services(path: Path) -> dict:
    # noqa justified: _ComposeLoader subclasses SafeLoader and only adds
    # constructors for compose's two merge tags.
    return yaml.load(path.read_text(), Loader=_ComposeLoader)["services"]  # noqa: S506


def _bytes(value: str) -> int:
    text = str(value).strip().lower().removesuffix("b")
    if text[-1] in _UNITS:
        return int(text[:-1]) * _UNITS[text[-1]]
    return int(text)


def test_every_production_service_rotates_its_logs():
    for name, service in _services(PROD).items():
        logging = service.get("logging")
        assert logging is not None, f"{name} has no logging configuration"
        assert logging["driver"] == "json-file", name
        options = logging["options"]
        max_size = _bytes(options["max-size"])
        max_file = int(options["max-file"])
        assert max_size > 0 and max_file > 0, name
        assert max_size * max_file <= MAX_LOG_BYTES_PER_SERVICE, name


def test_the_production_overlay_covers_every_service_in_the_base_file():
    """A service that exists only in the base file would ship unrotated."""
    assert set(_services(BASE)) == set(_services(PROD))


def test_memory_limits_use_the_form_compose_honours_outside_swarm():
    """`deploy.resources` is ignored without swarm; `mem_limit` is not."""
    for name, service in _services(PROD).items():
        assert "deploy" not in service, name


def test_the_limited_platform_leaves_the_host_room_for_a_coding_worker():
    services = _services(PROD)
    total = sum(_bytes(s["mem_limit"]) for s in services.values() if "mem_limit" in s)
    assert total <= MAX_TOTAL_MEM_BYTES, f"platform reserves {total} bytes"


def test_only_caddy_publishes_ports_to_the_host_beyond_the_loopback_admin():
    """The production contour: TLS through Caddy, admin on loopback, nothing else."""
    base = _services(BASE)
    prod = _services(PROD)
    published = {}
    for name in base:
        ports = prod[name]["ports"] if "ports" in prod[name] else base[name].get("ports", [])
        if ports:
            published[name] = ports
    assert set(published) == {"caddy", "admin-frontend"}
    assert published["caddy"] == ["80:80", "443:443"]
    assert all(str(port).startswith("127.0.0.1:") for port in published["admin-frontend"])
