import json
import re
from typing import Any


class ResultParseError(Exception):
    """Raised when result parsing fails."""

    pass


class ResultParser:
    """Parses agent execution output to extract structured results."""

    _RESULT_PATTERN = re.compile(r"<result>\s*(.*?)\s*</result>", re.DOTALL)

    @classmethod
    def parse(cls, stdout: str) -> dict[str, Any] | None:
        """
        Extract and parse JSON result from stdout.
        Returns None if no <result> tags found.
        Raises ResultParseError if JSON is invalid.
        """
        match = cls._RESULT_PATTERN.search(stdout)
        if not match:
            return None

        json_str = match.group(1)
        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ResultParseError(f"Invalid JSON in result block: {e}") from e
