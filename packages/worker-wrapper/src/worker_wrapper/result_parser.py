import json
import re
from typing import Any


class ResultParseError(Exception):
    """Raised when result parsing fails."""

    pass


_REJECT_REASON_MAX_LENGTH = 1000


class ResultParser:
    """Parses agent execution output to extract structured results.

    Handles three formats:
    1. Claude CLI JSON format: {"type": "result", "result": "...<result>JSON</result>..."}
    2. Raw text format: ...<result>JSON</result>...
    3. REJECTED marker: ## REJECTED followed by reason text
    """

    _RESULT_PATTERN = re.compile(r"<result>\s*(.*?)\s*</result>", re.DOTALL)
    _REJECTED_PATTERN = re.compile(r"^##\s+REJECTED\s*$", re.MULTILINE | re.IGNORECASE)

    @classmethod
    def parse(cls, stdout: str) -> dict[str, Any] | None:
        """
        Extract and parse JSON result from stdout.
        Returns None if no <result> tags and no ## REJECTED marker found.
        Raises ResultParseError if JSON is invalid.

        Priority: <result> tags > ## REJECTED marker.
        """
        # First, try to parse as Claude CLI JSON output
        text_to_search = cls._extract_result_text(stdout)

        match = cls._RESULT_PATTERN.search(text_to_search)
        if match:
            json_str = match.group(1)
            try:
                return json.loads(json_str)
            except json.JSONDecodeError as e:
                raise ResultParseError(f"Invalid JSON in result block: {e}") from e

        # No <result> tags — check for ## REJECTED marker
        rejected = cls._parse_rejected(text_to_search)
        if rejected is not None:
            return rejected

        return None

    @classmethod
    def _parse_rejected(cls, text: str) -> dict[str, Any] | None:
        """Detect ## REJECTED marker and extract reason.

        Returns {"status": "rejected", "reject_reason": "..."} or None.
        """
        match = cls._REJECTED_PATTERN.search(text)
        if not match:
            return None

        # Everything after the ## REJECTED line is the reason
        reason = text[match.end() :].strip()
        if len(reason) > _REJECT_REASON_MAX_LENGTH:
            reason = reason[:_REJECT_REASON_MAX_LENGTH]

        return {"status": "rejected", "reject_reason": reason}

    @classmethod
    def extract_text(cls, stdout: str) -> str | None:
        """Extract plain text from Claude CLI JSON output.

        When Claude returns {"type": "result", "result": "some text"} without
        <result> tags, this extracts the plain text directly.
        """
        try:
            data = json.loads(stdout)
            if isinstance(data, dict) and data.get("type") == "result":
                return data.get("result")
        except (json.JSONDecodeError, TypeError):
            pass
        return None

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
