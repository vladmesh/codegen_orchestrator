from __future__ import annotations

from scripts import stand_preflight


def test_stand_contour_uses_token_validation_not_host_session_paths(monkeypatch):
    checked: list[str] = []

    monkeypatch.setenv("LIVE_CONTOUR", "stand")
    monkeypatch.setattr(stand_preflight, "check_contour", lambda: ("contour", True, "stand"))
    monkeypatch.setattr(stand_preflight, "check_docker", lambda: ("docker", True, ""))
    monkeypatch.setattr(stand_preflight, "check_disk", lambda: ("disk", True, ""))
    monkeypatch.setattr(stand_preflight, "check_deploy_target", lambda *_: ("deploy", True, ""))
    monkeypatch.setattr(
        stand_preflight,
        "check_stand_token_credentials",
        lambda: checked.append("token") or ("stand token", True, ""),
    )
    monkeypatch.setattr(
        stand_preflight,
        "check_claude_session",
        lambda *_: checked.append("claude") or ("claude", True, ""),
    )
    monkeypatch.setattr(
        stand_preflight,
        "check_codex_session",
        lambda *_: checked.append("codex") or ("codex", True, ""),
    )

    assert stand_preflight.main() == 0
    assert checked == ["token"]


def test_non_stand_contour_retains_the_host_session_checks(monkeypatch):
    checked: list[str] = []

    monkeypatch.delenv("LIVE_CONTOUR", raising=False)
    monkeypatch.setattr(stand_preflight, "check_contour", lambda: ("contour", True, ""))
    monkeypatch.setattr(stand_preflight, "check_docker", lambda: ("docker", True, ""))
    monkeypatch.setattr(stand_preflight, "check_disk", lambda: ("disk", True, ""))
    monkeypatch.setattr(stand_preflight, "check_deploy_target", lambda *_: ("deploy", True, ""))
    monkeypatch.setattr(
        stand_preflight,
        "check_stand_token_credentials",
        lambda: checked.append("token") or ("stand token", True, ""),
    )
    monkeypatch.setattr(
        stand_preflight,
        "check_claude_session",
        lambda *_: checked.append("claude") or ("claude", True, ""),
    )
    monkeypatch.setattr(
        stand_preflight,
        "check_codex_session",
        lambda *_: checked.append("codex") or ("codex", True, ""),
    )

    assert stand_preflight.main() == 0
    assert checked == ["claude", "codex"]
