"""OpenAPI 3.1 specification generator from internal specs.

Generates a complete OpenAPI specification from:
- models.yaml → JSON Schema definitions in components/schemas
- domain specs → paths and operations
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from framework.generators.context import OperationContextBuilder
from framework.lib.env import get_repo_root
from framework.lib.fs import atomic_write_text
from framework.spec.loader import AllSpecs, load_specs
from framework.spec.operations import OperationSpec


def type_to_openapi_schema(type_str: str) -> dict[str, Any]:
    """Convert a Python type name (as produced by type_spec_to_python) to an OpenAPI schema."""
    # Primitive mapping must win before the capitalized-name heuristic below,
    # otherwise "UUID" is treated as a model name and yields a dangling $ref.
    mapping = {
        "int": {"type": "integer"},
        "string": {"type": "string"},
        "str": {"type": "string"},
        "bool": {"type": "boolean"},
        "float": {"type": "number"},
        "UUID": {"type": "string", "format": "uuid"},
    }
    if type_str in mapping:
        return mapping[type_str]

    # Otherwise a capitalized name is a model reference.
    if type_str and type_str[0].isupper():
        return {"$ref": f"#/components/schemas/{type_str}"}

    return {"type": "string"}


class OpenAPIGenerator:
    """Generate OpenAPI 3.1 specification from validated specs."""

    def __init__(self, specs: AllSpecs) -> None:
        """Initialize with validated specs."""
        self.specs = specs
        self.context_builder = OperationContextBuilder()

    def generate(
        self,
        title: str = "API",
        version: str = "1.0.0",
        description: str = "",
        service_name: str | None = None,
    ) -> dict[str, Any]:
        """Generate complete OpenAPI 3.1 specification."""
        openapi = {
            "openapi": "3.1.0",
            "info": {
                "title": title,
                "version": version,
                "description": description,
            },
            "paths": self._generate_paths(service_name),
            "components": {
                "schemas": self._generate_schemas(),
            },
        }
        return openapi

    def _generate_schemas(self) -> dict[str, Any]:
        """Generate JSON Schema definitions from models.

        Reuses the canonical ModelsSpec.to_json_schema() so schema shape and the
        required-field derivation match the Pydantic codegen exactly. Model schemas
        contain no $ref, so the definitions map straight into components/schemas.
        """
        return self.specs.models.to_json_schema()["definitions"]

    def _generate_paths(self, service_name: str | None = None) -> dict[str, Any]:
        """Generate OpenAPI paths from domains."""
        paths: dict[str, Any] = {}

        for domain in self.specs.domains.values():
            if service_name and domain.service_name != service_name:
                continue

            # Get REST prefix from domain config
            prefix = ""
            tags = []
            if domain.config.rest:
                prefix = domain.config.rest.prefix
                tags = domain.config.rest.tags

            for operation in domain.get_rest_operations():
                ctx = self.context_builder.build_for_rest(operation)

                path = f"{prefix}{ctx.path or ''}"
                if path.endswith("/") and len(path) > 1:
                    path = path[:-1]
                if not path:
                    path = "/"

                if path not in paths:
                    paths[path] = {}

                op_spec = self._operation_to_openapi(operation, ctx, tags)
                paths[path][ctx.http_method.lower()] = op_spec

        return paths

    def _operation_to_openapi(
        self,
        operation: OperationSpec,
        ctx,
        tags: list[str],
    ) -> dict[str, Any]:
        """Convert operation to OpenAPI operation."""
        openapi_op: dict[str, Any] = {
            "operationId": operation.name,
            "summary": operation.name.replace("_", " ").title(),
            "tags": tags,
            "responses": {},
        }

        # Path parameters
        if ctx.params:
            openapi_op["parameters"] = [
                {
                    "name": p.name,
                    "in": p.param_source,
                    "required": True,
                    "schema": type_to_openapi_schema(p.type),
                }
                for p in ctx.params
            ]

        # Request body
        if ctx.input_model:
            openapi_op["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": f"#/components/schemas/{ctx.input_model}"},
                    },
                },
            }

        # Response
        status = str(ctx.status_code)
        if ctx.output_model:
            response_schema: dict[str, Any] = {
                "$ref": f"#/components/schemas/{ctx.output_model}"
            }
            if ctx.response_many:
                response_schema = {"type": "array", "items": response_schema}
            openapi_op["responses"][status] = {
                "description": "Successful response",
                "content": {
                    "application/json": {"schema": response_schema},
                },
            }
        else:
            openapi_op["responses"][status] = {
                "description": "No content",
            }

        return openapi_op


def generate_openapi(
    repo_root: Path | None = None,
    output_path: Path | None = None,
    title: str = "API",
    version: str = "1.0.0",
    service_name: str | None = None,
) -> dict[str, Any]:
    """Generate OpenAPI spec and optionally write to file."""
    if repo_root is None:
        repo_root = get_repo_root()

    specs = load_specs(repo_root)
    generator = OpenAPIGenerator(specs)
    openapi = generator.generate(title=title, version=version, service_name=service_name)

    if output_path:
        atomic_write_text(output_path, json.dumps(openapi, indent=2))

    return openapi


def main() -> None:
    """CLI entrypoint for OpenAPI generation."""
    repo_root = get_repo_root()
    services_dir = repo_root / "services"

    if not services_dir.exists():
        print("No services directory found.")
        return

    generated_count = 0

    for service_dir in services_dir.iterdir():
        if not service_dir.is_dir():
            continue

        service_name = service_dir.name
        spec_dir = service_dir / "spec"
        has_specs = spec_dir.exists() and any(spec_dir.glob("*.yaml"))

        if not has_specs:
            continue

        output_path = service_dir / "docs" / "openapi.json"
        generate_openapi(
            repo_root=repo_root,
            output_path=output_path,
            title=f"{service_name.title()} API",
            service_name=service_name,
        )
        print(f"Generated OpenAPI spec for {service_name}: {output_path.relative_to(repo_root)}")
        generated_count += 1

    if generated_count == 0:
        print("No services with specs found.")


if __name__ == "__main__":
    main()
