"""Entrypoint in the released API image; fixture data stays outside code."""

import json
import os
from pathlib import Path

import uvicorn

from shared.log_config import setup_logging
from shared.stand_fake_platform import create_app


def main() -> None:
    setup_logging(service_name="stand-fake-platform")
    app = create_app(
        os.environ["STAND_PLATFORM_ADMIN_TOKEN"],
        json.loads(Path(os.environ["STAND_PLATFORM_FIXTURE_PATH"]).read_text()),
    )
    uvicorn.run(app, host="0.0.0.0", port=8000, access_log=False)  # noqa: S104 - internal compose network


if __name__ == "__main__":
    main()
