"""TypeScript type generator from internal specs.

Generates TypeScript interfaces from models.yaml for frontend type safety.
"""

from __future__ import annotations

from pathlib import Path

from framework.lib.env import get_repo_root
from framework.lib.fs import atomic_write_text
from framework.spec.loader import AllSpecs, load_specs
from framework.spec.models import FieldSpec
from framework.spec.types import EnumType, type_spec_to_typescript


def field_to_typescript(field: FieldSpec) -> str:
    """Convert FieldSpec to TypeScript type."""
    return type_spec_to_typescript(field.type_spec)


class TypeScriptGenerator:
    """Generate TypeScript types from validated specs."""

    def __init__(self, specs: AllSpecs) -> None:
        """Initialize with validated specs."""
        self.specs = specs

    def generate(self) -> str:
        """Generate complete TypeScript types file."""
        lines: list[str] = [
            "// Auto-generated TypeScript types from models.yaml",
            "// DO NOT EDIT MANUALLY",
            "",
        ]

        # Named string-literal type aliases for enum fields, referenced by the
        # interfaces below. Modern TS avoids `enum` (runtime cost, friction with
        # erasable-syntax / type-stripping); a `type` alias is reusable and erasable.
        for model_name, model_spec in self.specs.models.models.items():
            for field_name, field_spec in model_spec.fields.items():
                if isinstance(field_spec.type_spec, EnumType):
                    enum_name = self._enum_type_name(model_name, field_name)
                    lines.append(self._generate_enum(enum_name, field_spec.type_spec))
                    lines.append("")

        # Generate interfaces
        for model_name, model_spec in self.specs.models.models.items():
            # Base interface
            lines.append(self._generate_interface(model_name, model_name, model_spec.fields))
            lines.append("")

            # Variant interfaces
            for variant_name in model_spec.variants:
                variant_full_name = f"{model_name}{variant_name}"
                fields = model_spec.get_variant_fields(variant_name)
                lines.append(self._generate_interface(variant_full_name, model_name, fields))
                lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _enum_type_name(model_name: str, field_name: str) -> str:
        """Name of the generated type alias for an enum field."""
        return f"{model_name}{field_name.title()}"

    def _generate_enum(self, name: str, spec: EnumType) -> str:
        """Generate a TypeScript string-literal union type alias."""
        union = " | ".join(f'"{v}"' for v in spec.values)
        return f"export type {name} = {union};"

    def _generate_interface(
        self, name: str, model_name: str, fields: dict[str, FieldSpec]
    ) -> str:
        """Generate TypeScript interface.

        Enum fields reference their named type alias (keyed on the base model
        name so variants point at the same alias); other fields render inline.
        """
        props = []
        for field_name, field_spec in fields.items():
            if isinstance(field_spec.type_spec, EnumType):
                ts_type = self._enum_type_name(model_name, field_name)
            else:
                ts_type = field_to_typescript(field_spec)
            optional = "" if field_spec.is_required else "?"
            props.append(f"  {field_name}{optional}: {ts_type};")

        return f"export interface {name} {{\n" + "\n".join(props) + "\n}"


def generate_typescript(
    repo_root: Path | None = None,
    output_path: Path | None = None,
) -> str:
    """Generate TypeScript types and optionally write to file.

    Args:
        repo_root: Repository root path
        output_path: If provided, write to this path

    Returns:
        The generated TypeScript content
    """
    if repo_root is None:
        repo_root = get_repo_root()

    specs = load_specs(repo_root)
    generator = TypeScriptGenerator(specs)
    content = generator.generate()

    if output_path:
        atomic_write_text(output_path, content)

    return content


def main() -> None:
    """CLI entrypoint for TypeScript generation."""
    repo_root = get_repo_root()
    output_path = repo_root / "frontend" / "generated" / "types.ts"

    # If frontend directory doesn't exist, use shared
    if not (repo_root / "frontend").exists():
        output_path = repo_root / "shared" / "shared" / "generated" / "types.ts"

    generate_typescript(repo_root, output_path)
    print(f"Generated TypeScript types: {output_path}")


if __name__ == "__main__":
    main()
