#!/usr/bin/env -S uv run --with telethon --python 3.11 python
"""Authorize a Telethon session for the stand and store it as a GitHub secret.

The stand needs its own session string. Production's cannot be shared: two
clients on one auth key fight, and Telegram may revoke the authorization —
taking production's QA down with it. The app credentials, on the other hand, are
deliberately reused. `my.telegram.org` issues one app per account, and one app
serves any number of sessions.

Two steps, because the login code only exists between them:

    make_stand_session.py send-code --phone +370...
    make_stand_session.py sign-in --code 12345 [--password ...]

`send-code` leaves a half-authorized session in a local state file, mode 0600,
which `sign-in` completes and then deletes. The finished session string is piped
straight into `gh secret set` and never printed, never written to a file that
outlives the run, and never passed on a command line.

The app credentials are read from the production `.env` over SSH, so they do not
travel through a shell history either.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

REPO = "vladmesh/codegen_orchestrator"
ENVIRONMENT = "stand"
SECRET = "TELETHON_SESSION"  # noqa: S105 — the name of a secret, not its value
PROD_SSH = "deploy@5wce.l.time4vps.cloud"
PROD_ENV = "/opt/codegen_orchestrator/.env"
STATE = Path.home() / ".cache" / "stand-telethon-state.json"


def _app_credentials() -> tuple[int, str]:
    """Read TELETHON_API_ID/HASH from the production installation."""
    script = (
        f"grep -E '^TELETHON_API_(ID|HASH)=' {PROD_ENV} | sed 's/^TELETHON_API_//' | cut -d= -f1,2"
    )
    # A fixed command with no user input; ssh is resolved from PATH by design.
    out = subprocess.run(  # noqa: S603
        ["ssh", "-o", "BatchMode=yes", PROD_SSH, script],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    values = dict(line.split("=", 1) for line in out.strip().splitlines())
    return int(values["ID"]), values["HASH"]


def _client(session: str = "") -> TelegramClient:
    api_id, api_hash = _app_credentials()
    return TelegramClient(StringSession(session), api_id, api_hash)


def _save_state(session: str, phone: str, code_hash: str) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.touch(mode=0o600, exist_ok=True)
    STATE.chmod(0o600)
    STATE.write_text(
        json.dumps({"session": session, "phone": phone, "code_hash": code_hash}),
        encoding="utf-8",
    )


def send_code(phone: str) -> int:
    client = _client()
    client.connect()
    try:
        sent = client.send_code_request(phone)
        _save_state(client.session.save(), phone, sent.phone_code_hash)
    finally:
        client.disconnect()
    print(f"login code sent to {phone}; run: make_stand_session.py sign-in --code <code>")
    return 0


def sign_in(code: str, password: str | None, print_instead: bool) -> int:
    if not STATE.exists():
        print("No pending login. Run send-code first.", file=sys.stderr)
        return 2
    state = json.loads(STATE.read_text(encoding="utf-8"))

    client = _client(state["session"])
    client.connect()
    try:
        try:
            client.sign_in(phone=state["phone"], code=code, phone_code_hash=state["code_hash"])
        except SessionPasswordNeededError:
            if not password:
                print(
                    "The account has 2FA enabled: re-run sign-in with --password.",
                    file=sys.stderr,
                )
                return 2
            client.sign_in(password=password)
        me = client.get_me()
        session = client.session.save()
    finally:
        client.disconnect()

    STATE.unlink()
    print(f"authorized as {me.username or me.first_name} (id={me.id})")

    if print_instead:
        print(session)
        return 0

    # The session never becomes an argument: gh reads it from stdin.
    subprocess.run(  # noqa: S603
        ["gh", "secret", "set", SECRET, "--repo", REPO, "--env", ENVIRONMENT],  # noqa: S607
        input=session,
        text=True,
        check=True,
    )
    print(f"stored {SECRET} in environment '{ENVIRONMENT}' of {REPO}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    send = sub.add_parser("send-code", help="ask Telegram for a login code")
    send.add_argument("--phone", required=True)

    finish = sub.add_parser("sign-in", help="complete the login with that code")
    finish.add_argument("--code", required=True)
    finish.add_argument("--password", help="2FA password, if the account has one")
    finish.add_argument("--print-instead", action="store_true")

    args = parser.parse_args()
    if args.command == "send-code":
        return send_code(args.phone)
    return sign_in(args.code, args.password, args.print_instead)


if __name__ == "__main__":
    raise SystemExit(main())
