"""Stand-only selection shared by work admission and the deterministic QA consumer."""

import re

from shared.contracts.acceptance import parse_deterministic_qa_criteria

CRITERION = re.compile(r"^\s*-?\s*Stand conversation: ([a-z][a-z0-9-]{0,63})\s*$", re.MULTILINE)


def select_conversation(criteria, *, contour):
    matches = list(CRITERION.finditer(criteria))
    if not matches:
        if "Stand conversation" in criteria:
            raise ValueError("malformed conversation criterion")
        return None
    if len(matches) != 1 or "Stand mechanical" in criteria or contour != "stand":
        raise ValueError("one conversation in the stand contour is required")
    match = matches[0]
    return "conversation", match[1], criteria[: match.start()] + criteria[match.end() :]


def parse_stand_qa_criteria(criteria, *, contour):
    try:
        selected = select_conversation(criteria, contour=contour)
    except ValueError:
        return None
    return parse_deterministic_qa_criteria(selected[2] if selected else criteria)
