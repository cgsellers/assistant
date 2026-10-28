"""
Every route the app serves, crossed by a client that should not get through
(tenancy step 4.5, DESIGN §9.3).

Each route is given a kind, and the kind decides which crossover checks run
against it. The first test walks the application's routes and fails on any
route not listed here: a route added later cannot slip past isolation
unnoticed, because its author has to say what it exposes before the suite
passes again -- and saying so is what makes these checks run against it.

The kinds:

  public           answers without credentials; touches no client's data
  self             the caller's own session -- needs credentials, nothing else
  upload           creates a document owned by the caller
  client_document  reads one document by id -- a client gets its own only
  client_document_write
                   changes one document by id -- likewise, whatever is sent
  client_listing   reads many -- a client sees nothing of anyone else's
  settings_read    an organisation's keys and people: people only, own only
  settings_write   changing them: likewise, and another's look missing
  team             the processor API and the panel: the Ledgerline team only
  team_document    a panel page about one document: team only, and even a
                   team *key* (never staff) sees only the team's own
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount

from api.main import app
from core import auth, repository, tenancy, users
from core.models import OcrResult
from doctypes import registry, seed

from .conftest import TINY_PNG, demo_org_id

pytestmark = pytest.mark.usefixtures("fast_hashing")

PASSWORD = "correct horse battery"

ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/openapi.json"): "public",
    ("GET", "/docs"): "public",
    ("GET", "/docs/oauth2-redirect"): "public",
    ("GET", "/redoc"): "public",
    ("GET", "/health"): "public",
    ("GET", "/ready"): "public",
    ("POST", "/auth/login"): "public",
    ("GET", "/panel/login"): "public",
    ("POST", "/panel/login"): "public",
    ("POST", "/auth/invitation"): "public",
    ("POST", "/auth/invitation/accept"): "public",
    ("GET", "/settings/keys"): "settings_read",
    ("GET", "/settings/members"): "settings_read",
    ("POST", "/settings/keys"): "settings_write",
    ("DELETE", "/settings/keys/{key_id}"): "settings_write",
    ("POST", "/settings/members"): "settings_write",
    ("PATCH", "/settings/members/{member_id}"): "settings_write",
    ("DELETE", "/settings/members/{member_id}"): "settings_write",
    ("POST", "/auth/logout"): "self",
    ("GET", "/auth/me"): "self",
    ("POST", "/panel/logout"): "self",
    ("POST", "/documents"): "upload",
    ("POST", "/documents/base64"): "upload",
    ("GET", "/documents/{document_id}"): "client_document",
    ("GET", "/documents/{document_id}/ocr"): "client_document",
    ("GET", "/documents/{document_id}/file"): "client_document",
    ("GET", "/documents/{document_id}/result"): "client_document",
    ("GET", "/documents/{document_id}/fields"): "client_document",
    ("PATCH", "/documents/{document_id}/fields"): "client_document_write",
    ("GET", "/documents"): "client_listing",
    ("GET", "/documents/summary"): "client_listing",
    ("GET", "/documents/types"): "client_listing",
    ("GET", "/documents/export"): "client_listing",
    ("GET", "/costs"): "client_listing",
    ("GET", "/processors"): "team",
    ("POST", "/processors"): "team",
    ("POST", "/processors/draft-with-ai"): "team",
    ("GET", "/processors/{name}"): "team",
    ("GET", "/processors/{name}/versions/{version}"): "team",
    ("PUT", "/processors/{name}/versions/{version}"): "team",
    ("POST", "/processors/{name}/versions/{version}/publish"): "team",
    ("GET", "/processors/{name}/access"): "team",
    ("PUT", "/processors/{name}/access"): "team",
    ("POST", "/processors/{name}/retire"): "team",
    ("POST", "/processors/{name}/versions/{version}/trial"): "team",
    ("POST", "/processors/{name}/versions/{version}/trial/file"): "team",
    ("GET", "/panel"): "team",
    ("GET", "/panel/documents"): "team",
    ("GET", "/panel/documents/{document_id}"): "team_document",
    ("GET", "/panel/new"): "team",
    ("POST", "/panel/new"): "team",
    ("POST", "/panel/new/ai"): "team",
    ("GET", "/panel/processors/{name}"): "team",
    ("GET", "/panel/processors/{name}/diff"): "team",
    ("POST", "/panel/processors/{name}/access"): "team",
    ("GET", "/panel/processors/{name}/retire"): "team",
    ("POST", "/panel/processors/{name}/retire"): "team",
    ("GET", "/panel/processors/{name}/versions/{version}"): "team",
    ("POST", "/panel/processors/{name}/versions/{version}/draft"): "team",
    ("GET", "/panel/processors/{name}/versions/{version}/edit"): "team",
    ("POST", "/panel/processors/{name}/versions/{version}/save"): "team",
    ("GET", "/panel/processors/{name}/versions/{version}/try"): "team",
    ("POST", "/panel/processors/{name}/versions/{version}/try"): "team",
    ("GET", "/panel/processors/{name}/versions/{version}/publish"): "team",
    ("POST", "/panel/processors/{name}/versions/{version}/publish"): "team",
}

#: Not routes in the API sense: static files, public by design (core/auth.py).
MOUNTS = {"/panel/static"}


def _served() -> tuple[set[tuple[str, str]], set[str]]:
    """Every (method, path) the app answers, and every mount, found by walking it."""
    routes, mounts = set(), set()

    def walk(items):
        for item in items:
            if type(item).__name__ == "_IncludedRouter":
                walk(item.original_router.routes)
            elif isinstance(item, APIRoute):
                routes.update((method, item.path) for method in item.methods)
            elif isinstance(item, Mount):
                mounts.add(item.path)
            else:  # the docs and schema routes FastAPI adds itself
                routes.update(
                    (method, item.path) for method in (item.methods or ()) if method != "HEAD"
                )

    walk(app.router.routes)
    return routes, mounts


def _label(route: tuple[str, str]) -> str:
    return f"{route[0]} {route[1]}"


def _of(kind: str) -> list[tuple[str, str]]:
    return sorted(route for route, k in ROUTES.items() if k == kind)


def test_every_route_says_what_it_exposes():
    """The test that makes the rest automatic."""
    routes, mounts = _served()
    unclassified = routes - set(ROUTES)
    assert not unclassified, (
        f"routes with no tenancy kind: {sorted(unclassified)}. Add each to ROUTES in "
        "tests/test_every_route.py with the kind that describes what it exposes -- "
        "that is what makes the crossover checks run against it."
    )
    gone = set(ROUTES) - routes
    assert not gone, f"ROUTES lists routes that no longer exist: {sorted(gone)}"
    assert mounts == MOUNTS


# --- The world every check runs in ------------------------------------------


@pytest.fixture
def world(_wired, session):
    registry.clear_cache()
    seed.sync(session, seed.load_seeds())
    out = {}
    for slug in ("acme", "globex"):
        org = tenancy.create(session, slug.title(), slug)
        document, _ = repository.ingest(
            session, organisation_id=org.id, data=TINY_PNG + slug.encode(),
            mime_type="image/png", original_filename=f"{slug}-secret.png",
        )
        session.add(OcrResult(
            document_id=document.id, engine="fake", engine_version="1", params={},
            full_text=f"{slug} confidential", layout_blocks=[], mean_confidence=None,
        ))
        repository.record_extraction(
            session, document_id=document.id, doc_type="receipt", schema_version=1,
            model_used="m", raw_json={"vendor": f"{slug} vendor"}, cost_usd=Decimal("0.01"),
        )
        key_row, key = auth.create(session, f"{slug} erp", organisation_id=org.id, role="admin")
        person = users.create(session, f"bo@{slug}.test", PASSWORD, organisation_id=org.id,
                              is_staff=False, role="admin")
        out[slug] = {
            "org": org, "document": document.id, "key": key,
            "key_id": key_row.id, "member_id": person.id,
        }
    users.create(session, "ana@ledgerline.test", PASSWORD,
                 organisation_id=demo_org_id(session), is_staff=True, role="admin")
    _, out["team_key"] = auth.create(
        session, "team script", organisation_id=demo_org_id(session), role="admin"
    )
    yield out
    registry.clear_cache()


def _path(template: str, world) -> str:
    return (
        template.replace("{document_id}", world["globex"]["document"])
        .replace("{key_id}", world["globex"]["key_id"])
        .replace("{member_id}", world["globex"]["member_id"])
        .replace("{name}", "receipt")
        .replace("{version}", "1")
    )


def _key(key: str) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


def _signed_in(email: str) -> tuple[TestClient, dict]:
    """A browser signed in through /auth/login -- the door a portal will use."""
    browser = TestClient(app, base_url="https://testserver")
    me = browser.post("/auth/login", json={"email": email, "password": PASSWORD}).json()
    return browser, {"X-CSRF-Token": me.get("csrf_token", "")}


def _call(client: TestClient, method: str, path: str, headers=None):
    return client.request(method, path, headers=headers or {}, follow_redirects=False)


# --- The checks, one per kind -------------------------------------------------


@pytest.mark.parametrize("route", _of("public"), ids=_label)
def test_public_routes_answer_without_credentials(world, anon_client, route):
    method, path = route
    assert _call(anon_client, method, path).status_code != 401


@pytest.mark.parametrize(
    "route",
    _of("self")
    + _of("settings_read")
    + _of("settings_write")
    + _of("client_document")
    + _of("client_document_write")
    + _of("client_listing")
    + _of("upload"),
    ids=_label,
)
def test_everything_else_needs_credentials(world, anon_client, route):
    method, path = route
    response = _call(anon_client, method, _path(path, world))
    # The panel's pages redirect a browser to sign in; the API says 401.
    assert response.status_code in (401, 303)


@pytest.mark.parametrize("route", _of("client_document"), ids=_label)
def test_another_clients_document_is_exactly_a_missing_one(world, route):
    method, path = route
    with _key(world["acme"]["key"]) as acme:
        theirs = _call(acme, method, _path(path, world))
        missing = _call(acme, method, path.replace("{document_id}", "no-such-document"))
    assert theirs.status_code == 404
    assert theirs.content == missing.content


@pytest.mark.parametrize("route", _of("client_document"), ids=_label)
def test_staff_do_reach_it_so_the_404_is_isolation_not_breakage(world, route):
    method, path = route
    browser, _ = _signed_in("ana@ledgerline.test")
    with browser:
        assert _call(browser, method, _path(path, world)).status_code == 200


#: A well-formed change, so what answers is the document check, not the body's.
CHANGE = {"revision": 1, "values": {}}


@pytest.mark.parametrize("route", _of("client_document_write"), ids=_label)
def test_another_clients_document_cannot_be_changed_and_looks_missing(world, route):
    method, path = route
    with _key(world["acme"]["key"]) as acme:
        theirs = acme.request(method, _path(path, world), json=CHANGE)
        missing = acme.request(
            method, path.replace("{document_id}", "no-such-document"), json=CHANGE
        )
    assert theirs.status_code == 404
    assert theirs.content == missing.content


@pytest.mark.parametrize("route", _of("client_document_write"), ids=_label)
def test_staff_do_reach_a_change_so_the_404_is_isolation(world, route):
    method, path = route
    browser, csrf = _signed_in("ana@ledgerline.test")
    with browser:
        response = browser.request(method, _path(path, world), json=CHANGE, headers=csrf)
    assert response.status_code not in (401, 403, 404)


#: What each settings change sends, so the answer is about who asks, not the body.
SETTINGS_BODY = {
    "/settings/keys": {"name": "probe"},
    "/settings/members": {"email": "probe@example.test", "role": "viewer"},
    "/settings/members/{member_id}": {"role": "viewer"},
}


@pytest.mark.parametrize("route", _of("settings_read") + _of("settings_write"), ids=_label)
def test_settings_are_for_people_not_keys(world, route):
    method, path = route
    with _key(world["acme"]["key"]) as acme:
        response = acme.request(method, _path(path, world), json=SETTINGS_BODY.get(path))
    assert response.status_code == 403


@pytest.mark.parametrize("route", _of("settings_read"), ids=_label)
def test_settings_show_nothing_of_another_client(world, route):
    method, path = route
    browser, _ = _signed_in("bo@acme.test")
    with browser:
        body = _call(browser, method, path).text
    assert "bo@acme.test" in body or "acme erp" in body
    for marker in ("Globex", "bo@globex.test", "globex erp", world["globex"]["member_id"]):
        assert marker not in body, f"{marker!r} leaked through {method} {path}"


@pytest.mark.parametrize(
    "route", [r for r in _of("settings_write") if "{" in r[1]], ids=_label
)
def test_another_clients_key_or_person_looks_missing(world, route):
    method, path = route
    browser, csrf = _signed_in("bo@acme.test")
    missing = path.replace("{key_id}", "no-such-key").replace("{member_id}", "no-such-member")
    with browser:
        body = SETTINGS_BODY.get(path)
        theirs = browser.request(method, _path(path, world), json=body, headers=csrf)
        nobody = browser.request(method, missing, json=body, headers=csrf)
    assert theirs.status_code == 404
    assert theirs.content == nobody.content


@pytest.mark.parametrize("route", _of("client_listing"), ids=_label)
def test_a_listing_shows_nothing_of_another_client(world, route):
    method, path = route
    with _key(world["acme"]["key"]) as acme:
        body = _call(acme, method, path).text
    globex = world["globex"]
    markers = (globex["document"], globex["org"].id, "Globex", "globex-secret", "globex vendor")
    for marker in markers:
        assert marker not in body, f"{marker!r} leaked through {method} {path}"


@pytest.mark.parametrize("route", _of("team") + _of("team_document"), ids=_label)
def test_a_clients_key_is_refused_the_teams_tools(world, route):
    method, path = route
    with _key(world["acme"]["key"]) as acme:
        assert _call(acme, method, _path(path, world)).status_code == 403


@pytest.mark.parametrize("route", _of("team") + _of("team_document"), ids=_label)
def test_so_is_a_clients_signed_in_user(world, route):
    """With a valid session and CSRF token, so the 403 is the team check itself."""
    method, path = route
    browser, csrf = _signed_in("bo@acme.test")
    with browser:
        assert _call(browser, method, _path(path, world), csrf).status_code == 403


@pytest.mark.parametrize("route", _of("team_document"), ids=_label)
def test_even_the_teams_key_sees_only_the_teams_documents(world, route):
    """Keys are never staff: the team's key is scoped to the team's organisation."""
    method, path = route
    with _key(world["team_key"]) as team:
        assert _call(team, method, _path(path, world)).status_code == 404


@pytest.mark.parametrize("route", _of("upload"), ids=_label)
def test_an_upload_belongs_to_the_uploader_even_for_another_clients_bytes(
    world, route, session
):
    import base64

    method, path = route
    payload = TINY_PNG + b"globex"  # exactly the bytes Globex already uploaded
    with _key(world["acme"]["key"]) as acme:
        if path.endswith("base64"):
            response = acme.post(path, json={"content_b64": base64.b64encode(payload).decode()})
        else:
            response = acme.post(path, files={"file": ("x.png", payload, "image/png")})
    created = response.json()
    assert created["id"] != world["globex"]["document"]
    assert created["duplicate"] is False
    owner = repository.get_document(session, created["id"]).organisation_id
    assert owner == world["acme"]["org"].id
