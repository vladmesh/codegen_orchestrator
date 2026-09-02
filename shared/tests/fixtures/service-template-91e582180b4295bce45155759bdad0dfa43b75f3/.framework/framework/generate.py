"""Unified code generator entrypoint.

Uses modular generators with validated spec types.
"""

from pathlib import Path
import sys

from framework.generators.controllers import ControllersGenerator
from framework.generators.event_adapter import EventAdapterGenerator
from framework.generators.events import EventsGenerator
from framework.generators.jobs_manifest import JobsManifestGenerator
from framework.generators.protocols import ProtocolsGenerator
from framework.generators.routers import RoutersGenerator
from framework.generators.schemas import SchemasGenerator
from framework.generators.settings_manifest import SettingsManifestGenerator
from framework.lib.env import get_repo_root
from framework.spec.loader import SpecValidationError, load_specs


def generate_all(repo_root: Path | None = None) -> None:
    """Run all generators."""
    if repo_root is None:
        repo_root = get_repo_root()

    print("Loading and validating specs...")
    try:
        specs = load_specs(repo_root)
    except SpecValidationError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not specs.models.models:
        print("No specs found. Skipping generation.")
        return

    print(f"  Models: {len(specs.models.models)}")
    print(f"  Domains: {len(specs.domains)}")
    print(f"  Events: {len(specs.events.events)}")

    # Run generators in order
    generators = [
        ("Schemas", SchemasGenerator(specs, repo_root)),
        ("SettingsManifests", SettingsManifestGenerator(specs, repo_root)),
        ("JobsManifests", JobsManifestGenerator(specs, repo_root)),
        ("Protocols", ProtocolsGenerator(specs, repo_root)),
        ("Controllers", ControllersGenerator(specs, repo_root)),
        ("Events", EventsGenerator(specs, repo_root)),
        ("EventAdapters", EventAdapterGenerator(specs, repo_root)),
        ("Routers", RoutersGenerator(specs, repo_root)),
    ]

    for name, generator in generators:
        print(f"\nGenerating {name}...")
        generated = generator.generate()
        for path in generated:
            print(f"  ✓ {path.relative_to(repo_root)}")
        if not generated:
            print("  (no files generated)")

    print("\n✓ Generation complete!")


def main() -> None:
    """CLI entrypoint."""
    generate_all()


if __name__ == "__main__":
    main()
