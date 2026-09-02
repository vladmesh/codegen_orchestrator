"""PO tools — the Product Brief the user confirms before any story exists.

The requirement becomes durable typed data *before* the story, and it becomes it
exactly once. Two tools do that, and both are thin over the released API
(`services/api/src/routers/product_briefs.py`); neither stores a brief anywhere
of its own:

* `present_product_brief` opens the revision and returns the exact text the user
  is shown. It is the only composer. Asked a second time — a retry, a restart,
  another PO turn — it returns the *stored* revision rather than composing a
  second interpretation of the same conversation.
* `confirm_product_brief` freezes that revision by echoing the stored content
  back to the server, which refuses anything but a byte-for-byte match.

**How a restart cannot lose the presentation.** The brief is addressed by an id
the server mints, and until the brief is bound to a story there is no route that
finds it again from the project alone. So the project's config carries the one
pointer, `product_brief_id`, naming the revision presented for this project and
not yet spent. A PO that restarts reads that pointer before it composes
anything. `create_story` clears it once the brief is bound, because from then on
`GET /product-briefs/by-story/{story_id}` is the way back to it.

**What makes a retry a retry.** The creation key is a fingerprint of the
document being presented, not a guessed revision number. The server owns the
revision counter, and the PO forgets its pointer as soon as a brief is bound, so
a guess would collide with a spent key on a project's second brief and refuse
every later presentation. A fingerprint answers the question the key is actually
asked — "is this the same presentation?" — without knowing anything the PO does
not hold.

**A correction is a new revision, never an edit.** The released API has no
update path. When the user corrects the brief, the PO presents again naming the
revision it corrects, a second revision is opened, and the pointer moves. The
superseded revision stays exactly as it was.
"""

from __future__ import annotations

from hashlib import sha256
from http import HTTPStatus
import json
import uuid

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import ValidationError
import structlog

from shared.contracts.dto.product_brief import (
    ProductBriefConfirm,
    ProductBriefCreate,
    ProductBriefRead,
    ProposedProductBriefContent,
)

from .tools_shared import _get_api, _user_headers

logger = structlog.get_logger(__name__)

#: Where the project config carries the revision presented and not yet spent.
PRODUCT_BRIEF_POINTER_KEY = "product_brief_id"

#: How an unchosen value is shown to the user. Unchanged from the prose
#: confirmation this flow replaces: the user reads one message and sees which
#: values nobody has decided yet.
NOT_SPECIFIED = "not specified"


#: How many keys one presentation may try before giving up. Each step past the
#: first means the key it would have used already names a revision that is spent
#: — bound to a story — so the ceiling is only ever reached by a project that
#: presented the identical document for that many stories in a row.
MAX_PRESENTATION_KEYS = 8


def _creation_request_id(
    project_id: str, title: str, content: ProposedProductBriefContent, attempt: int = 0
) -> str:
    """The idempotency key of one presentation of one project's brief.

    Derived from what is actually being presented — the project, the title and
    the document — and not from a guessed revision number. A guess cannot be
    unique: the project's revision counter lives on the server, the PO forgets
    the pointer once a brief is bound, and a second brief on that project would
    guess a number the released endpoint has already spent and be refused with
    409 for the rest of the project's life.

    A fingerprint has the idempotency the guess was reaching for and none of
    that: a retry after a crash — or after the PO process was replaced — sends
    the same key for the same document and the released endpoint answers it with
    the revision it already opened, while a different document is a different
    key and therefore a new revision.

    `attempt` distinguishes the one case a fingerprint alone cannot: the same
    document presented again for a *second* story, after the first revision was
    bound. `present_product_brief` walks it upwards only when the key it tried
    answered with a revision that is already spent.
    """
    document = json.dumps(
        {"title": title, "content": content.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    fingerprint = sha256(document.encode()).hexdigest()[:32]
    suffix = "" if attempt == 0 else f":{attempt + 1}"
    return f"po-brief:{project_id}:{fingerprint}{suffix}"


def _confirmation_request_id(brief_id: str) -> str:
    """The idempotency key of the confirmation of one revision."""
    return f"po-brief-confirm:{brief_id}"


def _render(brief: ProductBriefRead) -> str:
    """The one atomic confirmation message, and the only text the user is shown.

    Everything the user is asked to confirm is in it: the summary, every
    must-requirement with the id the architect will dispose of it by and the
    provenance of its wording, and every typed initial setting. It is not a
    series of questions, and it ends the way the user is told to answer it.
    """
    lines = [
        f"<b>{brief.title}</b>",
        f"Product Brief revision {brief.revision} (id: {brief.id})",
        "",
        f"Summary: {brief.content.summary}",
        "",
        "Must-requirements:",
    ]
    for requirement in brief.content.must_requirements:
        lines.append(f"- [{requirement.id}] {requirement.text}")
        if requirement.user_wording:
            lines.append(f'  your words: "{requirement.user_wording}"')
        elif requirement.wording_reference:
            lines.append(f"  said in: {requirement.wording_reference}")
        else:
            lines.append(f"  source: {NOT_SPECIFIED}")
    lines.append("")
    lines.append("Initial settings:")
    if not brief.content.initial_settings:
        lines.append(f"- {NOT_SPECIFIED}")
    for setting in brief.content.initial_settings:
        subject = "" if setting.subject_id is None else f", subject {setting.subject_id}"
        value = (
            NOT_SPECIFIED
            if setting.value is None
            else json.dumps(setting.value, ensure_ascii=False)
        )
        lines.append(f"- {setting.key} ({setting.scope.value}{subject}) = {value}")
    lines.append("")
    lines.append("yes / correct me")
    return "\n".join(lines)


async def _load_brief(brief_id: str, headers: dict[str, str]) -> ProductBriefRead | None:
    """The stored revision, or None when the id names nothing we may read."""
    response = await _get_api().get_raw(f"product-briefs/{brief_id}", headers=headers)
    if response.status_code == HTTPStatus.NOT_FOUND:
        return None
    response.raise_for_status()
    return ProductBriefRead.model_validate(response.json())


async def _project_config(project_id: str, headers: dict[str, str]) -> dict:
    response = await _get_api().get_raw(f"projects/{project_id}", headers=headers)
    response.raise_for_status()
    return response.json().get("config") or {}


async def _write_pointer(
    project_id: str, config: dict, brief_id: str | None, headers: dict[str, str]
) -> None:
    """Point the project at the revision in flight, or at none at all."""
    updated = dict(config)
    if brief_id is None:
        updated.pop(PRODUCT_BRIEF_POINTER_KEY, None)
    else:
        updated[PRODUCT_BRIEF_POINTER_KEY] = brief_id
    if updated == config:
        return
    response = await _get_api().patch_raw(
        f"projects/{project_id}", json={"config": updated}, headers=headers
    )
    response.raise_for_status()


async def clear_brief_pointer(project_id: str, headers: dict[str, str]) -> None:
    """Forget the presented revision, once it is bound to a story.

    Called by `create_story` after the bind, because from then on the brief is
    reachable by its story and the pointer would only name a spent revision.
    """
    config = await _project_config(project_id, headers)
    if config.get(PRODUCT_BRIEF_POINTER_KEY):
        await _write_pointer(project_id, config, None, headers)


def _presented(brief: ProductBriefRead, prefix: str) -> str:
    return f"{prefix}\n\n{_render(brief)}"


@tool
async def present_product_brief(
    project_id: str,
    title: str,
    summary: str,
    must_requirements: list[dict],
    initial_settings: list[dict] | None = None,
    corrects_brief_id: str | None = None,
    *,
    config: RunnableConfig,
) -> str:
    """Open the Product Brief revision the user is asked to confirm, and show it.

    Call this ONCE, after the requirements are gathered and before any
    `create_story` for new product work. Send the whole message this tool
    returns to the user, unchanged, and wait for their answer. On "yes" call
    `confirm_product_brief`. On a correction call this tool again with
    `corrects_brief_id` set to the brief id it returned — a correction is a new
    revision, never an edit.

    If a revision is already open for this project, this tool returns that
    stored revision and composes nothing: do not re-word it, show what it
    returned.

    Args:
        project_id: Project ID (UUID).
        title: Short name of the product, e.g. "Reading tracker bot".
        summary: What the product is, in one or two sentences.
        must_requirements: One entry per thing the product must do:
            `{"id": "r1", "text": "It stores a book",
              "user_wording": "<the user's own words>"}`.
            The id is used in a URL, so use only letters, digits, `.`, `_`, `-`.
            Give either `user_wording` (what the user actually wrote) or
            `wording_reference` (where they wrote it) — exactly one, never both,
            never neither.
        initial_settings: Typed values the product should start with:
            `{"key": "alerts.default_currency", "scope": "product",
              "value": "USD"}`. Leave empty when the user chose none. NEVER put
            a token, password, API key or any other secret here — secrets go to
            `set_project_secret`.
        corrects_brief_id: The brief id the user corrected, when re-presenting
            after a correction. Leave unset the first time.
    """
    try:
        project_uuid = uuid.UUID(project_id)
    except ValueError:
        return (
            f"No Product Brief was presented: {project_id!r} is not a project UUID. "
            "Use the UUID create_project returned."
        )
    api = _get_api()
    headers = _user_headers(config)
    project_config = await _project_config(project_id, headers)
    pointer = project_config.get(PRODUCT_BRIEF_POINTER_KEY)

    stored = await _load_brief(pointer, headers) if pointer else None
    if stored is not None and corrects_brief_id is None:
        if stored.confirmed_at is not None:
            return _presented(
                stored,
                "This brief is already confirmed. Do not present it again — call "
                f"create_story(product_brief_id='{stored.id}'). It reads:",
            )
        return _presented(
            stored,
            "This project already has a presented Product Brief revision, and this is "
            "it — not a new one. Send it to the user as it stands:",
        )
    if stored is not None and corrects_brief_id != stored.id:
        return _presented(
            stored,
            f"Brief {corrects_brief_id} is not the revision presented for this project; "
            f"revision {stored.revision} ({stored.id}) superseded it. Correct that one "
            "instead. It reads:",
        )
    if stored is None and corrects_brief_id is not None:
        return (
            f"No Product Brief revision is open for project {project_id}, so "
            f"{corrects_brief_id} cannot be corrected. Present the brief without "
            "corrects_brief_id."
        )

    try:
        content = ProposedProductBriefContent.model_validate(
            {
                "summary": summary,
                "must_requirements": must_requirements,
                "initial_settings": initial_settings or [],
            }
        )
    except ValidationError as invalid:
        logger.warning("po_brief_content_refused", project_id=project_id, error=str(invalid))
        return f"No Product Brief was presented — the content is not valid:\n{invalid}"

    if refusal := await _refuse_settings_that_are_secrets(project_id, content, headers):
        return refusal

    brief = None
    for attempt in range(MAX_PRESENTATION_KEYS):
        creation = ProductBriefCreate(
            project_id=project_uuid,
            title=title,
            content=content,
            request_id=_creation_request_id(project_id, title, content, attempt),
        )
        response = await api.post_raw(
            "product-briefs/", json=creation.model_dump(mode="json"), headers=headers
        )
        if response.status_code == HTTPStatus.CONFLICT:
            # Two presentations raced for the same next revision number. Nothing
            # was opened and nothing was lost; the same call made again wins or
            # finds the revision the other one opened.
            return (
                "No Product Brief was presented: "
                f"{response.json().get('detail')}. Nothing was changed — call "
                "present_product_brief again."
            )
        response.raise_for_status()
        candidate = ProductBriefRead.model_validate(response.json())
        if candidate.story_id is None:
            # Either a revision just opened, or the one this exact presentation
            # opened before and has not spent yet. Both are this presentation.
            brief = candidate
            break
        # The key named a revision already bound to a story: this project has
        # been asked for the same document twice, once per story. Reach past it
        # rather than re-presenting something no new story may be planned from.
    if brief is None:
        logger.warning("po_brief_presentation_keys_exhausted", project_id=project_id)
        return (
            f"No Product Brief was presented: every one of the last "
            f"{MAX_PRESENTATION_KEYS} revisions of this project holds exactly this "
            "document and is already bound to a story. Ask the user what is different "
            "about this one before presenting it again."
        )
    await _write_pointer(project_id, project_config, brief.id, headers)
    if brief.confirmed_at is not None:
        # The key found a revision this project confirmed and has not spent —
        # a presentation whose pointer was lost after the user already said yes.
        # Asking them again would be asking them to confirm what they confirmed.
        return _presented(
            brief,
            "This brief is already confirmed. Do not present it again — call "
            f"create_story(product_brief_id='{brief.id}'). It reads:",
        )
    logger.info(
        "po_product_brief_presented",
        project_id=project_id,
        brief_id=brief.id,
        revision=brief.revision,
        corrects_brief_id=corrects_brief_id,
    )
    return _presented(brief, "Send this to the user exactly as it stands and wait for an answer:")


async def _refuse_settings_that_are_secrets(
    project_id: str, content: ProposedProductBriefContent, headers: dict[str, str]
) -> str | None:
    """A value this project holds as a secret is never one of its settings.

    The typed vocabulary already refuses a credential-shaped key or value. This
    asks the second question, the one only the project can answer: is this key
    one of the secrets the PO stored for this project? The secret *values* are
    never read — only their names — so nothing here can put credential material
    into a document an LLM reads back.
    """
    if not content.initial_settings:
        return None
    response = await _get_api().get_raw(
        f"projects/{project_id}/config/secrets/keys", headers=headers
    )
    response.raise_for_status()
    secret_keys = {key.upper() for key in response.json().get("keys", [])}
    for setting in content.initial_settings:
        candidates = {
            setting.key.replace(".", "_").upper(),
            setting.key.rsplit(".", maxsplit=1)[-1].upper(),
        }
        if candidates & secret_keys:
            logger.warning(
                "po_brief_setting_is_a_secret", project_id=project_id, setting_key=setting.key
            )
            return (
                f"No Product Brief was presented: '{setting.key}' is a secret of this "
                "project, and a secret is never a setting. Remove it from "
                "initial_settings — it is already stored with set_project_secret."
            )
    return None


@tool
async def confirm_product_brief(project_id: str, brief_id: str, *, config: RunnableConfig) -> str:
    """Freeze the presented Product Brief after the user answered yes.

    Call this only once the user confirmed the exact message
    `present_product_brief` returned. The stored revision is echoed back to the
    server, which refuses anything but a byte-for-byte match, so a brief the
    user never saw cannot be confirmed. After this, create the story with
    `create_story(project_id, title, description, product_brief_id=<brief_id>)`.

    Args:
        project_id: Project ID (UUID).
        brief_id: The brief id `present_product_brief` returned.
    """
    api = _get_api()
    headers = _user_headers(config)
    brief = await _load_brief(brief_id, headers)
    if brief is None:
        return f"No Product Brief {brief_id} exists. Present one first."
    if str(brief.project_id) != project_id:
        return f"Product Brief {brief_id} belongs to another project; nothing was confirmed."
    if brief.confirmed_at is not None:
        return (
            f"Product Brief {brief.id} (revision {brief.revision}) is already confirmed. "
            f"Create the story with create_story(product_brief_id='{brief.id}')."
        )

    confirmation = ProductBriefConfirm(
        request_id=_confirmation_request_id(brief.id),
        content=ProposedProductBriefContent.model_validate(brief.content.model_dump(mode="json")),
    )
    response = await api.post_raw(
        f"product-briefs/{brief.id}/confirm",
        json=confirmation.model_dump(mode="json"),
        headers=headers,
    )
    if response.status_code == HTTPStatus.CONFLICT:
        return (
            f"Product Brief {brief.id} was not confirmed: {response.json().get('detail')}. "
            "Present the stored revision again before asking the user anything."
        )
    response.raise_for_status()
    logger.info("po_product_brief_confirmed", project_id=project_id, brief_id=brief.id)
    return (
        f"Product Brief {brief.id} (revision {brief.revision}) is confirmed and frozen. "
        f"Now call create_story(project_id, title, description, product_brief_id='{brief.id}')."
    )


__all__ = [
    "NOT_SPECIFIED",
    "PRODUCT_BRIEF_POINTER_KEY",
    "clear_brief_pointer",
    "confirm_product_brief",
    "present_product_brief",
]
