"""Controller stub generator.

Generates controller stubs only if they don't exist.
Uses OperationContextBuilder for unified context building.
"""

from pathlib import Path

from framework.generators.base import BaseGenerator
from framework.generators.context import OperationContextBuilder, controller_path


class ControllersGenerator(BaseGenerator):
    """Generate controller stubs for services."""

    def __init__(self, *args, **kwargs) -> None:
        """Initialize generator."""
        super().__init__(*args, **kwargs)
        self.context_builder = OperationContextBuilder()

    def generate(self) -> list[Path]:
        """Generate controller stubs (only if not existing)."""
        generated = []

        for _domain_key, domain in sorted(self.specs.domains.items()):
            output_file = controller_path(self.repo_root, domain)

            # Only generate if file doesn't exist
            if output_file.exists():
                continue

            self._generate_controller(domain, output_file)
            generated.append(output_file)

        return generated

    def _generate_controller(self, domain, output_file: Path) -> None:
        """Generate a single controller stub."""
        handlers = []
        imports: set[str] = set()
        param_type_imports: set[str] = set()

        for operation in domain.operations:
            ctx = self.context_builder.build_for_protocol(operation)
            imports.update(ctx.imports)
            param_type_imports.update(ctx.param_type_imports)
            handlers.append(ctx)

        # Controllers are editable, don't add generated header
        self.render_to_file(
            "controller.py.j2",
            output_file,
            add_header=False,
            controller_class_name=domain.controller_class_name,
            protocol_name=domain.protocol_name,
            handlers=handlers,
            imports=imports,
            param_type_imports=sorted(param_type_imports),
        )
