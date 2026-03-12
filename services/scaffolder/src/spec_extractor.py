"""Extract spec summaries from a scaffolded project workspace.

Parses YAML spec files (models, events, domain operations) and produces
a compact summary dict suitable for storing in project config.
"""

from __future__ import annotations

from pathlib import Path

import structlog
import yaml

logger = structlog.get_logger(__name__)


def extract_specs_summary(workspace: Path) -> dict:
    """Extract a compact spec summary from a scaffolded project.

    Returns a dict with:
        models: list of {name, fields: [field_name: type_str, ...]}
        events: list of {name, message, publish, subscribe}
        domains: list of {service, domain, operations: [{name, method, path, input, output}]}

    Returns empty dict if no spec files found.
    """
    summary: dict = {}

    models = _extract_models(workspace / "shared" / "spec" / "models.yaml")
    if models:
        summary["models"] = models

    events = _extract_events(workspace / "shared" / "spec" / "events.yaml")
    if events:
        summary["events"] = events

    domains = _extract_domains(workspace)
    if domains:
        summary["domains"] = domains

    if summary:
        logger.info(
            "specs_extracted",
            models=len(summary.get("models", [])),
            events=len(summary.get("events", [])),
            domains=len(summary.get("domains", [])),
        )

    return summary


def _type_to_str(t) -> str:
    """Convert a spec type definition to a compact string."""
    if isinstance(t, str):
        return t
    if isinstance(t, dict):
        type_name = t.get("type", "unknown")
        if type_name == "enum":
            values = t.get("values", [])
            return f"enum({', '.join(str(v) for v in values)})"
        if type_name == "list":
            inner = _type_to_str(t.get("of", "unknown"))
            return f"list[{inner}]"
        if type_name == "dict":
            key = _type_to_str(t.get("key", "string"))
            val = _type_to_str(t.get("value", "unknown"))
            return f"dict[{key}, {val}]"
        if type_name == "optional":
            inner = _type_to_str(t.get("of", "unknown"))
            return f"optional[{inner}]"
        return type_name
    return str(t)


def _extract_models(path: Path) -> list[dict]:
    """Extract model summaries from models.yaml."""
    data = _load_yaml(path)
    if not data or "models" not in data:
        return []

    result = []
    for name, model_def in sorted(data["models"].items()):
        fields_raw = model_def.get("fields", {})
        fields = {}
        for fname, fdef in fields_raw.items():
            if isinstance(fdef, dict):
                type_str = _type_to_str(fdef.get("type", "unknown"))
                modifiers = []
                if fdef.get("readonly"):
                    modifiers.append("readonly")
                if fdef.get("optional"):
                    modifiers.append("optional")
                if modifiers:
                    type_str += f" ({', '.join(modifiers)})"
                fields[fname] = type_str
            else:
                fields[fname] = str(fdef)

        variants = list(model_def.get("variants", {}).keys())

        entry: dict = {"name": name, "fields": fields}
        if variants:
            entry["variants"] = variants
        result.append(entry)

    return result


def _extract_events(path: Path) -> list[dict]:
    """Extract event summaries from events.yaml."""
    data = _load_yaml(path)
    if not data or "events" not in data:
        return []

    result = []
    for name, event_def in sorted(data["events"].items()):
        result.append(
            {
                "name": name,
                "message": event_def.get("message", ""),
                "publish": event_def.get("publish", False),
                "subscribe": event_def.get("subscribe", False),
            }
        )
    return result


def _extract_domains(workspace: Path) -> list[dict]:
    """Extract domain operation summaries from per-service spec files."""
    result = []
    services_dir = workspace / "services"
    if not services_dir.is_dir():
        return []

    for service_dir in sorted(services_dir.iterdir()):
        if not service_dir.is_dir():
            continue
        spec_dir = service_dir / "spec"
        if not spec_dir.is_dir():
            continue

        service_name = service_dir.name
        for spec_file in sorted(spec_dir.glob("*.yaml")):
            if spec_file.name == "manifest.yaml":
                continue
            data = _load_yaml(spec_file)
            if not data or "operations" not in data:
                continue

            domain_name = data.get("domain", spec_file.stem)
            prefix = ""
            config = data.get("config", {})
            if isinstance(config, dict):
                rest_config = config.get("rest", {})
                if isinstance(rest_config, dict):
                    prefix = rest_config.get("prefix", "")

            ops = []
            for op_name, op_def in data["operations"].items():
                op_summary: dict = {"name": op_name}
                rest = op_def.get("rest", {})
                if rest:
                    op_summary["method"] = rest.get("method", "")
                    op_summary["path"] = rest.get("path", "")
                if op_def.get("input"):
                    op_summary["input"] = op_def["input"]
                if op_def.get("output"):
                    op_summary["output"] = op_def["output"]
                events = op_def.get("events", {})
                if events.get("subscribe"):
                    op_summary["subscribes"] = events["subscribe"]
                if events.get("publish_on_success"):
                    op_summary["publishes"] = events["publish_on_success"]
                ops.append(op_summary)

            entry: dict = {"service": service_name, "domain": domain_name, "operations": ops}
            if prefix:
                entry["prefix"] = prefix
            result.append(entry)

    return result


def _load_yaml(path: Path) -> dict | None:
    """Load a YAML file, returning None on any error."""
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return yaml.safe_load(f)
    except Exception:
        logger.warning("spec_yaml_load_failed", path=str(path), exc_info=True)
        return None
