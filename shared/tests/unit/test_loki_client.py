"""Unit tests for Loki client — response parsing logic."""

import json

from shared.clients.loki import LokiClient

SAMPLE_LOKI_RESPONSE = {
    "status": "success",
    "data": {
        "resultType": "streams",
        "result": [
            {
                "stream": {
                    "job": "docker",
                    "com_codegen_project_id": "abc-123",
                    "compose_service": "backend",
                },
                "values": [
                    [
                        "1711000000000000000",
                        json.dumps(
                            {
                                "event": "request",
                                "method": "GET",
                                "path": "/api/health",
                                "status_code": 200,
                                "duration_ms": 12.5,
                                "user_id": "tg:42",
                            }
                        ),
                    ],
                    [
                        "1711000001000000000",
                        json.dumps(
                            {
                                "event": "request",
                                "method": "POST",
                                "path": "/api/items",
                                "status_code": 500,
                                "duration_ms": 450.0,
                                "user_id": "tg:99",
                            }
                        ),
                    ],
                ],
            },
            {
                "stream": {
                    "job": "docker",
                    "com_codegen_project_id": "abc-123",
                    "compose_service": "tg_bot",
                },
                "values": [
                    [
                        "1711000002000000000",
                        json.dumps(
                            {
                                "event": "request",
                                "update_type": "message",
                                "command": "/start",
                                "duration_ms": 30.0,
                                "user_id": "tg:42",
                            }
                        ),
                    ],
                ],
            },
        ],
    },
}


def test_parse_response_returns_flat_list():
    entries = LokiClient._parse_response(SAMPLE_LOKI_RESPONSE)
    assert len(entries) == 3


def test_parse_response_preserves_fields():
    entries = LokiClient._parse_response(SAMPLE_LOKI_RESPONSE)
    first = entries[0]
    assert first["event"] == "request"
    assert first["method"] == "GET"
    assert first["path"] == "/api/health"
    assert first["status_code"] == 200
    assert first["duration_ms"] == 12.5
    assert first["user_id"] == "tg:42"


def test_parse_response_adds_labels():
    entries = LokiClient._parse_response(SAMPLE_LOKI_RESPONSE)
    assert entries[0]["_labels"]["compose_service"] == "backend"
    assert entries[2]["_labels"]["compose_service"] == "tg_bot"


def test_parse_response_handles_non_json_lines():
    data = {
        "data": {
            "result": [
                {
                    "stream": {"job": "docker"},
                    "values": [
                        ["1711000000000000000", "plain text log line"],
                    ],
                }
            ]
        }
    }
    entries = LokiClient._parse_response(data)
    assert len(entries) == 1
    assert entries[0]["raw"] == "plain text log line"


def test_parse_response_empty_result():
    data = {"data": {"result": []}}
    entries = LokiClient._parse_response(data)
    assert entries == []
