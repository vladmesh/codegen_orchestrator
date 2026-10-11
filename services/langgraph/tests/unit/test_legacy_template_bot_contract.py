"""A legacy service-template bot deploys without the kit users grant capability."""

import pytest

from src.subgraphs.devops.secret_resolver import SecretResolverNode, TypedSecretResolutionError


class _EmptyContract:
    entries: dict = {}


def test_legacy_template_bot_needs_no_generated_grant_capability():
    spec = {
        "config": {
            "modules": ["backend", "tg_bot"],
            "service_template": {"source": "gh:vladmesh/service-template", "commit": "0.3.6"},
        }
    }

    SecretResolverNode._validate_generated_bot_capability(spec, _EmptyContract())


def test_kit_bot_still_needs_generated_grant_capability():
    spec = {
        "config": {
            "modules": ["backend", "tg_bot"],
            "service_template": {"source": "gh:vladmesh/codegen-product-kit", "commit": "2.5.0"},
        }
    }

    with pytest.raises(TypedSecretResolutionError):
        SecretResolverNode._validate_generated_bot_capability(spec, _EmptyContract())
