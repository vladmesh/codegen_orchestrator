"""Generate event adapters from controller protocols."""

from pathlib import Path

from framework.generators.base import BaseGenerator
from framework.generators.context import OperationContextBuilder


class EventAdapterGenerator(BaseGenerator):
    """Generate event adapters using controller protocols."""

    def __init__(self, *args, **kwargs) -> None:
        """Initialize generator."""
        super().__init__(*args, **kwargs)
        self.context_builder = OperationContextBuilder()

    def generate(self) -> list[Path]:
        """Generate event adapters for all services with event operations."""
        generated_files = []

        # Group domains by service and collect event operations
        services_data: dict[str, dict] = {}

        for _domain_key, domain in sorted(self.specs.domains.items()):
            service_name = domain.service_name

            # Get operations that have events configured
            events_ops = domain.get_events_operations()
            if not events_ops:
                continue

            if service_name not in services_data:
                services_data[service_name] = {
                    "domains": [],
                    "imports": set(),
                }

            handlers = []

            for operation in events_ops:
                ctx = self.context_builder.build_for_events(operation)

                # Only process operations with subscribe (incoming events)
                if not ctx.subscribe_channel:
                    continue

                handlers.append(ctx)

                # Collect imports
                services_data[service_name]["imports"].update(ctx.imports)

            if handlers:
                services_data[service_name]["domains"].append(
                    {
                        "name": domain.name,
                        "protocol_name": domain.protocol_name,
                        "handlers": handlers,
                    }
                )

        # Generate event_adapter.py for each service with event handlers
        for service_name, data in services_data.items():
            if not data["domains"]:
                continue

            output_file = (
                self.repo_root
                / "services"
                / service_name
                / "src"
                / "generated"
                / "event_adapter.py"
            )

            self.render_to_file(
                "event_adapter.py.j2",
                output_file,
                service_name=service_name,
                domains=data["domains"],
                imports=data["imports"],
            )
            generated_files.append(output_file)

        return generated_files
