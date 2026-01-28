import json
import re
from typing import Any


class ResultParseError(Exception):
    """Raised when result parsing fails."""

    pass


class ResultParser:
    """Parses agent execution output to extract structured results.

    Handles two formats:
    1. Claude CLI JSON format: {"type": "result", "result": "...<result>JSON</result>..."}
    2. Raw text format: ...<result>JSON</result>...
    """

    _RESULT_PATTERN = re.compile(r"<result>\s*(.*?)\s*</result>", re.DOTALL)

    @classmethod
    def parse(cls, stdout: str) -> dict[str, Any] | None:
        """
        Extract and parse JSON result from stdout.
        Returns None if no <result> tags found.
        Raises ResultParseError if JSON is invalid.
        """
        # First, try to parse as Claude CLI JSON output
        text_to_search = cls._extract_result_text(stdout)

        match = cls._RESULT_PATTERN.search(text_to_search)
        if not match:
            return None

        json_str = match.group(1)
        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ResultParseError(f"Invalid JSON in result block: {e}") from e

    @classmethod
    def _extract_result_text(cls, stdout: str) -> str:
        """
        Extract the text content to search for <result> tags.

        If stdout is Claude CLI JSON output, extract the 'result' field.
        Otherwise, return stdout as-is.
        """
        try:
            data = json.loads(stdout)
            if isinstance(data, dict) and "result" in data:
                # Claude CLI format - get the result field
                return data["result"]
        except (json.JSONDecodeError, TypeError):
            pass

        # Not Claude CLI format, or parsing failed - return as-is
        return stdout
