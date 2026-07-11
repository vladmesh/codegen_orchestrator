from pathlib import Path


def test_production_compose_has_no_local_service_template_mount():
    compose = Path("docker-compose.yml").read_text()
    assert "SERVICE_TEMPLATE_PATH" not in compose
    assert "/data/service-template" not in compose
