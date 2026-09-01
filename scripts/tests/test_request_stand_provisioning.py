import json

from scripts import request_stand_provisioning


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b'{"request_id":"request-1"}'


def test_stand_profile_is_sent_as_an_explicit_request_body(monkeypatch):
    captured = {}
    monkeypatch.setenv("INTERNAL_API_KEY", "internal-key")
    monkeypatch.setattr(
        "sys.argv",
        [
            "request_stand_provisioning.py",
            "--handle",
            "bitlaunch-one",
            "--profile",
            "stand_e2e",
        ],
    )

    def open_request(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(request_stand_provisioning.urllib.request, "urlopen", open_request)

    assert request_stand_provisioning.main() == 0
    assert captured["timeout"] == 30
    assert json.loads(captured["request"].data) == {"profile": "stand_e2e"}
    assert captured["request"].headers["Content-type"] == "application/json"
