"""Protocol generator for controllers.

Generates typing.Protocol classes from domain specifications.
"""

from pathlib import Path

from framework.generators.base import BaseGenerator
from framework.generators.context import OperationContextBuilder


class ProtocolsGenerator(BaseGenerator):
    """Generate controller protocols from domain specs."""

    def __init__(self, *args, **kwargs) -> None:
        """Initialize generator."""
        super().__init__(*args, **kwargs)
        self.context_builder = OperationContextBuilder()

    def generate(self) -> list[Path]:
        """Generate protocols for all services."""
        generated_files = []

        # Group domains by service
        services_domains: dict[str, list] = {}
        services_imports: dict[str, set[str]] = {}
        services_param_type_imports: dict[str, set[str]] = {}

        for _domain_key, domain in sorted(self.specs.domains.items()):
            service_name = domain.service_name

            if service_name not in services_domains:
                services_domains[service_name] = []
                services_imports[service_name] = set()
                services_param_type_imports[service_name] = set()

            handlers = []
            for operation in domain.operations:
                ctx = self.context_builder.build_for_protocol(operation)

                # Collect imports
                services_imports[service_name].update(ctx.imports)
                services_param_type_imports[service_name].update(ctx.param_type_imports)

                handlers.append(ctx)

            services_domains[service_name].append(
                {
                    "name": domain.name,
                    "protocol_name": domain.protocol_name,
                    "handlers": handlers,
                }
            )

        # Generate protocols.py for each service
        for service_name, domains_context in services_domains.items():
            output_file = (
                self.repo_root / "services" / service_name / "src" / "generated" / "protocols.py"
            )

            self.render_to_file(
                "protocols.py.j2",
                output_file,
                domains=domains_context,
                imports=services_imports[service_name],
                param_type_imports=sorted(services_param_type_imports[service_name]),
            )
            generated_files.append(output_file)

        return generated_files
