"""Events generator for FastStream publishers."""

from pathlib import Path

from framework.generators.base import BaseGenerator


class EventsGenerator(BaseGenerator):
    """Generate event publishers."""

    def __init__(self, *args, **kwargs) -> None:
        """Initialize with output path."""
        super().__init__(*args, **kwargs)
        self.output_file = self.repo_root / "shared" / "shared" / "generated" / "events.py"

    def generate(self) -> list[Path]:
        """Generate events module.

        Always generates at least a stub with get_broker() so the import
        never fails and coding agents have a clear place to add events.
        """
        publishers = []
        imports: set[str] = set()

        for event in self.specs.events.events:
            event_ctx = {
                "name": event.name,
                "message_model": event.message,
                "subject": event.name.replace("_", "."),  # user_created -> user.created
            }
            imports.add(event.message)

            if event.publish:
                publishers.append(event_ctx)

        self.render_to_file(
            "events.py.j2",
            self.output_file,
            publishers=publishers,
            imports=imports,
        )

        return [self.output_file]
