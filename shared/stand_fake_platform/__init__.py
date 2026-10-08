"""In-memory stand fixture service. No production process starts this app."""

from datetime import UTC, datetime
import re
import secrets

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Product(BaseModel):
    display_name: str
    orchestrator_project_id: str
    disabled: bool = False


class Grant(BaseModel):
    scopes: list[str]
    quota: dict[str, int]


class Key(BaseModel):
    key: str = Field(pattern=r"^cps_[a-z2-7]{12}_[A-Za-z0-9_-]{43}$", repr=False)
    label: str


class FixtureRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: str
    path: str
    status: int = Field(ge=100, le=599)
    body: object


class Fixture(BaseModel):
    model_config = ConfigDict(extra="forbid")
    routes: list[FixtureRoute]


def _route_pattern(template: str) -> re.Pattern:
    if not template.startswith("/") or "?" in template:
        raise ValueError("fixture paths must be absolute paths without query strings")
    parts = re.split(r"(\{[A-Za-z_][A-Za-z0-9_]*\})", template)
    return re.compile(
        "^" + "".join("[^/]+" if p.startswith("{") else re.escape(p) for p in parts) + "$"
    )


def _render(value: object, now: str) -> object:
    if isinstance(value, str):
        return value.replace("{now}", now)
    if isinstance(value, list):
        return [_render(item, now) for item in value]
    if isinstance(value, dict):
        return {key: _render(item, now) for key, item in value.items()}
    return value


def _bearer(request: Request) -> str:
    authorization = request.headers.get("Authorization", "")
    return authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""


def _public_key(key: dict) -> dict:
    return {k: v for k, v in key.items() if k != "key"}


class FakePlatform:
    """One process owns ephemeral products, grants and key registrations."""

    def __init__(self, admin_token: str, fixture: dict):
        if not admin_token:
            raise ValueError("STAND_PLATFORM_ADMIN_TOKEN is required")
        self.admin_token = admin_token
        self.routes = [
            (_route_pattern(route.path), route) for route in Fixture.model_validate(fixture).routes
        ]
        self.products: dict[str, dict] = {}
        self.key_owners: dict[str, str] = {}

    @staticmethod
    def ensure_new(product: dict | None, kind: str, identity: str) -> None:
        if kind != "keys" and product is not None and (not kind or identity in product["grants"]):
            raise HTTPException(412)

    async def admin(self, request: Request, product_id: str, suffix: str = ""):
        if not secrets.compare_digest(_bearer(request), self.admin_token):
            raise HTTPException(401)
        product = self.products.get(product_id)
        if request.method == "GET":
            if suffix or product is None:
                raise HTTPException(404)
            return JSONResponse(
                {
                    **{k: v for k, v in product.items() if k not in {"keys", "grants"}},
                    "grants": product["grants"],
                    "keys": [_public_key(key) for key in product["keys"].values()],
                }
            )
        kind, _, identity = suffix.partition("/")
        if suffix and (kind not in {"grants", "keys"} or not identity or "/" in identity):
            raise HTTPException(404)
        if kind != "keys":
            if request.headers.get("If-None-Match") != "*":
                raise HTTPException(428)
            self.ensure_new(product, kind, identity)
        if suffix and product is None:
            raise HTTPException(404)
        try:
            body = await request.json()
            # Reading a streamed body yields. Recheck against the latest state;
            # validation and mutation below are synchronous and cannot interleave.
            product = self.products.get(product_id)
            self.ensure_new(product, kind, identity)
            if not suffix:
                self.products[product_id] = {
                    **Product.model_validate(body).model_dump(),
                    "grants": {},
                    "keys": {},
                }
                return JSONResponse(None, status_code=201)
            if kind == "grants":
                product["grants"][identity] = Grant.model_validate(body).model_dump()
                return JSONResponse(None, status_code=201)
            key = Key.model_validate(body)
        except (ValueError, ValidationError):
            raise HTTPException(422) from None
        return self.register(product_id, identity, key)

    def register(self, product_id: str, identity: str, key: Key) -> JSONResponse:
        if key.key.split("_")[1] != identity:
            raise HTTPException(422)
        owner = self.key_owners.get(identity)
        if owner is not None and owner != product_id:
            raise HTTPException(409)
        product = self.products[product_id]
        existing = product["keys"].get(identity)
        if existing is not None and existing["key"] != key.key:
            raise HTTPException(409)
        if existing is None:
            existing = {**key.model_dump(), "key_id": identity, "revoked_at": None}
            product["keys"][identity] = existing
            self.key_owners[identity] = product_id
        return JSONResponse(_public_key(existing))

    async def serve(self, request: Request, service: str, path: str):
        token = _bearer(request)
        identity = (
            token.split("_")[1]
            if re.fullmatch(r"cps_[a-z2-7]{12}_[A-Za-z0-9_-]{43}", token)
            else None
        )
        owner = self.key_owners.get(identity)
        if owner is None:
            raise HTTPException(401)
        product = self.products[owner]
        key = product["keys"][identity]
        if not secrets.compare_digest(key["key"], token) or key["revoked_at"] is not None:
            raise HTTPException(401)
        if product["disabled"] or service not in product["grants"]:
            raise HTTPException(403)
        for pattern, route in self.routes:
            if route.method == request.method and pattern.fullmatch(request.url.path):
                return JSONResponse(
                    _render(route.body, datetime.now(UTC).isoformat()), status_code=route.status
                )
        raise HTTPException(404)


def create_app(admin_token: str, fixture: dict) -> FastAPI:
    """Build isolated state with typed fixture input and exact Bearer authentication."""
    fake = FakePlatform(admin_token, fixture)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.products = fake.products
    app.add_api_route("/admin/v1/products/{product_id}", fake.admin, methods=["GET", "PUT"])
    app.add_api_route(
        "/admin/v1/products/{product_id}/{suffix:path}", fake.admin, methods=["GET", "PUT"]
    )
    app.add_api_route(
        "/{service}/{path:path}", fake.serve, methods=["GET", "POST", "PUT", "DELETE", "PATCH"]
    )
    return app
