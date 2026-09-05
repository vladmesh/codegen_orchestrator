"""Unified spec loader with validation.

Loads all YAML specs and validates them using Pydantic models.
Provides clear error messages for spec violations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError
import yaml

from framework.spec.events import EventsSpec
from framework.spec.manifests import ServiceManifest, parse_service_manifest
from framework.spec.models import ModelsSpec
from framework.spec.operations import DomainSpec, unwrap_list


class SpecValidationError(Exception):
    """Raised when spec validation fails."""

    def __init__(self, message: str, file_path: str | None = None) -> None:
        self.file_path = file_path
        self.message = message
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        if self.file_path:
            return f"{self.file_path}: {self.message}"
        return self.message


@dataclass
class AllSpecs:
    """Container for all loaded and validated specs."""

    models: ModelsSpec
    events: EventsSpec
    domains: dict[str, DomainSpec] = field(default_factory=dict)
    manifests: dict[str, ServiceManifest] = field(default_factory=dict)


def load_yaml_file(file_path: Path) -> dict[str, Any]:
    """Load and parse a YAML file."""
    if not file_path.exists():
        raise SpecValidationError("File not found", str(file_path))

    try:
        with file_path.open() as f:
            data = yaml.safe_load(f)
            return data or {}
    except yaml.YAMLError as e:
        raise SpecValidationError(f"Invalid YAML syntax: {e}", str(file_path)) from e


def format_pydantic_error(error: ValidationError, context: str = "") -> str:
    """Format Pydantic validation error for human readability."""
    messages = []
    for err in error.errors():
        loc = ".".join(str(x) for x in err["loc"])
        msg = err["msg"]
        if context:
            messages.append(f"{context}.{loc}: {msg}")
        else:
            messages.append(f"{loc}: {msg}")
    return "\n".join(messages)


def load_models(models_file: Path) -> ModelsSpec:
    """Load and validate models.yaml."""
    data = load_yaml_file(models_file)

    try:
        return ModelsSpec.from_yaml(data)
    except ValidationError as e:
        raise SpecValidationError(format_pydantic_error(e, "models"), str(models_file)) from e
    except ValueError as e:
        raise SpecValidationError(str(e), str(models_file)) from e


def load_events(events_file: Path) -> EventsSpec:
    """Load and validate events.yaml."""
    if not events_file.exists():
        return EventsSpec(events=[])

    data = load_yaml_file(events_file)

    try:
        return EventsSpec.from_yaml(data)
    except ValidationError as e:
        raise SpecValidationError(format_pydantic_error(e, "events"), str(events_file)) from e
    except ValueError as e:
        raise SpecValidationError(str(e), str(events_file)) from e


def load_domain(domain_file: Path) -> DomainSpec:
    """Load and validate a domain spec."""
    data = load_yaml_file(domain_file)

    domain_name = domain_file.stem
    try:
        return DomainSpec.from_yaml(domain_name, data)
    except ValidationError as e:
        raise SpecValidationError(format_pydantic_error(e, "domain"), str(domain_file)) from e
    except ValueError as e:
        raise SpecValidationError(str(e), str(domain_file)) from e


def load_manifest(manifest_file: Path) -> ServiceManifest:
    """Load a service manifest without treating it as a domain spec."""
    data = load_yaml_file(manifest_file)
    try:
        return parse_service_manifest(data)
    except ValidationError as e:
        raise SpecValidationError(format_pydantic_error(e, "manifest"), str(manifest_file)) from e
    except ValueError as e:
        raise SpecValidationError(str(e), str(manifest_file)) from e


def extract_base_model(model_ref: str) -> str:
    """Extract base model name from a model reference.

    Handles:
        - "User" -> "User"
        - "list[User]" -> "User"
        - "List[User]" -> "User"
    """
    return unwrap_list(model_ref)[1]


def validate_model_references(
    models: ModelsSpec,
    domains: dict[str, DomainSpec],
    events: EventsSpec,
) -> list[str]:
    """Validate that all model references exist.

    Returns list of error messages.
    """
    errors = []
    known_models = models.get_model_names()

    # Check domains
    for domain_key, domain in domains.items():
        for op in domain.operations:
            if op.input_model:
                base_input = extract_base_model(op.input_model)
                if base_input not in known_models:
                    errors.append(
                        f"Domain '{domain_key}', operation '{op.name}': "
                        f"Unknown input model '{op.input_model}'"
                    )
            if op.output_model:
                base_output = extract_base_model(op.output_model)
                if base_output not in known_models:
                    errors.append(
                        f"Domain '{domain_key}', operation '{op.name}': "
                        f"Unknown output model '{op.output_model}'"
                    )

    # Check events
    for event in events.events:
        if event.message and event.message not in known_models:
            errors.append(f"Event '{event.name}': Unknown message model '{event.message}'")

    return errors


def _load_service_specs(services_dir: Path) -> dict[str, DomainSpec]:
    """Load domain specs from all services."""
    domains: dict[str, DomainSpec] = {}

    if not services_dir.exists():
        return domains

    for service_dir in services_dir.iterdir():
        if not service_dir.is_dir():
            continue

        spec_dir = service_dir / "spec"
        if not spec_dir.exists():
            continue

        service_name = service_dir.name

        # Legacy manifests are intentionally ignored.
        for spec_file in spec_dir.glob("*.yaml"):
            if spec_file.stem == "manifest":
                continue
            domain_key = f"{service_name}/{spec_file.stem}"
            domain = load_domain(spec_file)
            domain.service_name = service_name
            domains[domain_key] = domain

    return domains


def _load_service_manifests(services_dir: Path) -> dict[str, ServiceManifest]:
    """Load explicit service manifests from their dedicated top-level path."""
    manifests: dict[str, ServiceManifest] = {}
    if not services_dir.exists():
        return manifests

    for service_dir in services_dir.iterdir():
        if not service_dir.is_dir():
            continue
        manifest_file = service_dir / "manifest.yaml"
        if manifest_file.exists():
            manifests[service_dir.name] = load_manifest(manifest_file)
    return manifests


def _validate_unique_declarations(
    manifests: dict[str, ServiceManifest], field: str, label: str
) -> list[str]:
    """Reject a declared name claimed by more than one service manifest."""
    owners: dict[str, str] = {}
    errors: list[str] = []
    for service_name, manifest in manifests.items():
        for key in getattr(manifest, field)["properties"]:
            if key in owners:
                errors.append(
                    f"{label} '{key}' is declared by both '{owners[key]}' and '{service_name}'"
                )
            else:
                owners[key] = service_name
    return errors


def validate_manifest_settings(manifests: dict[str, ServiceManifest]) -> list[str]:
    """Reject settings keys declared by more than one service."""
    return _validate_unique_declarations(manifests, "settings_schema", "Setting")


def validate_manifest_jobs(manifests: dict[str, ServiceManifest]) -> list[str]:
    """Reject fireable job names declared by more than one service."""
    return _validate_unique_declarations(manifests, "jobs_schema", "Job")


def load_specs(repo_root: Path) -> AllSpecs:
    """Load and validate all specs from the repository.

    Args:
        repo_root: Path to the repository root

    Returns:
        AllSpecs containing validated models, events, and domains

    Raises:
        SpecValidationError: If any spec is invalid
    """
    services_dir = repo_root / "services"
    manifests = _load_service_manifests(services_dir)
    manifest_errors = validate_manifest_settings(manifests) + validate_manifest_jobs(manifests)
    if manifest_errors:
        message = "Manifest settings validation failed:\n" + "\n".join(
            f"  - {error}" for error in manifest_errors
        )
        raise SpecValidationError(
            message
        )

    # 1. Load models (required)
    shared_spec_dir = repo_root / "shared" / "spec"
    models_file = shared_spec_dir / "models.yaml"

    if not models_file.exists():
        return AllSpecs(
            models=ModelsSpec(models={}),
            events=EventsSpec(events=[]),
            manifests=manifests,
        )

    models = load_models(models_file)

    # 2. Load events (optional)
    events_file = shared_spec_dir / "events.yaml"
    events = load_events(events_file)

    # 3. Load service domains
    domains = _load_service_specs(services_dir)

    # 4. Explicit manifests are independent of ordinary domain generation.

    # 5. Cross-validate model references
    reference_errors = validate_model_references(models, domains, events)
    if reference_errors:
        raise SpecValidationError(
            "Model reference validation failed:\n" + "\n".join(f"  - {e}" for e in reference_errors)
        )

    return AllSpecs(models=models, events=events, domains=domains, manifests=manifests)


def validate_specs_cli(repo_root: Path) -> tuple[bool, str]:
    """CLI-friendly validation that returns success status and message.

    Returns:
        (success, message) tuple
    """
    try:
        specs = load_specs(repo_root)
        if not specs.models.models:
            return True, "No specs found. Skipping validation."
        model_count = len(specs.models.models)
        domain_count = len(specs.domains)
        event_count = len(specs.events.events)
        manifest_count = len(specs.manifests)
        return True, (
            f"Spec validation PASSED.\n"
            f"  Models: {model_count}\n"
            f"  Domains: {domain_count}\n"
            f"  Events: {event_count}\n"
            f"  Manifests: {manifest_count}"
        )
    except SpecValidationError as e:
        return False, f"Spec validation FAILED:\n  {e}"
