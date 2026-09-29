"""
T8 per-route request body limits, through nginx (docs/plans/t8-image-support.md §2.4, §4.12).

FastAPI reads a whole request body before any auth check or handler runs, so nginx's
per-route limits are the only real gate on body size. The rest of the suite talks to the
backend on port 8000 and never exercises them. Runs only with --nginx-url:

    cd tests/integration && pytest test_t8_nginx_limits.py --nginx-url http://localhost:8080

Each check sends a body just within, and just past, its route's nginx limit:
- everything else under /api/: 1 MiB (nginx's default, stated explicitly)
- image upload and replace: 11 MiB (a 10 MiB image plus multipart overhead)
- the two import routes: 41 MiB (a 40 MiB bundle plus multipart overhead)

"Reaches the backend" means the response is the backend's JSON (usually its own refusal of the
junk sent), not nginx's HTML 413 page.
"""

from __future__ import annotations

import io
import uuid

import httpx
import pytest
from PIL import Image

MIB = 1024 * 1024


@pytest.fixture(scope="module")
def nginx_url(request) -> str:
    url = request.config.getoption("--nginx-url")
    if not url:
        pytest.skip("Needs --nginx-url (for example http://localhost:8080)")
    return url.rstrip("/")


@pytest.fixture(scope="module")
def target(nginx_url: str, base_url: str, admin_token: str):
    """A game with one image, created directly on the backend; removed afterwards."""
    headers = {"Authorization": f"Bearer {admin_token}"}
    with httpx.Client(base_url=base_url, timeout=60.0, headers=headers) as backend:
        tag = uuid.uuid4().hex[:6]
        course = backend.post(
            "/api/admin/courses", json={"name": f"T8 nginx {tag}", "semester": "T8"}
        ).json()["id"]
        game = backend.post(
            "/api/admin/games", json={"title": f"T8 nginx {tag}", "course_id": course}
        ).json()["id"]
        buffer = io.BytesIO()
        Image.new("RGB", (20, 20), "red").save(buffer, "PNG")
        image = backend.post(
            f"/api/host/games/{game}/images",
            files={"file": ("a.png", buffer.getvalue(), "image/png")},
            data={"description": "Target"},
        ).json()["id"]
        yield {"course": course, "game": game, "image": image, "headers": headers}
        backend.delete(f"/api/admin/games/{game}")


def send(nginx_url: str, method: str, path: str, **kwargs) -> httpx.Response:
    with httpx.Client(base_url=nginx_url, timeout=120.0) as client:
        return client.request(method, path, **kwargs)


def refused_by_nginx(r: httpx.Response) -> bool:
    return r.status_code == 413 and "text/html" in r.headers.get("content-type", "")


def reached_backend(r: httpx.Response) -> bool:
    return r.headers.get("content-type", "").startswith("application/json")


def test_default_limit_on_other_api_routes(nginx_url, target):
    big = "x" * (2 * MIB)
    r = send(nginx_url, "POST", "/api/auth/login", json={"username": big, "password": "x"})
    assert refused_by_nginx(r), (r.status_code, r.text[:200])
    r = send(
        nginx_url,
        "POST",
        f"/api/host/games/{target['game']}/questions",
        headers=target["headers"],
        json={"type": "true_false", "grading_type": "ACCURACY", "prompt": big,
              "config": {}, "answer_data": {}},
    )
    assert refused_by_nginx(r), (r.status_code, r.text[:200])


def test_upload_and_replace_routes_accept_10_mib(nginx_url, target):
    upload = f"/api/host/games/{target['game']}/images"
    replace = f"/api/host/games/{target['game']}/images/{target['image']}"
    for method, path, form in (
        ("POST", upload, {"description": "Big"}),
        ("PUT", replace, {}),
    ):
        # Exactly the 10 MiB input limit: through nginx to the backend (a JSON refusal of
        # the junk bytes, 415, rather than nginx's page).
        r = send(
            nginx_url,
            method,
            path,
            headers=target["headers"],
            files={"file": ("big.bin", b"\0" * (10 * MIB), "application/octet-stream")},
            data=form,
        )
        assert reached_backend(r), (method, r.status_code, r.text[:200])
        assert r.status_code == 415, r.text
        # 12 MiB: refused by nginx before the backend sees it.
        r = send(
            nginx_url,
            method,
            path,
            headers=target["headers"],
            files={"file": ("big.bin", b"\0" * (12 * MIB), "application/octet-stream")},
            data=form,
        )
        assert refused_by_nginx(r), (method, r.status_code, r.text[:200])


@pytest.mark.parametrize("path", ["/api/host/games/import", "/api/admin/games/import"])
def test_import_routes_accept_40_mib(nginx_url, target, path):
    # Exactly the 40 MiB import limit reaches the backend (a JSON 422: not a bundle).
    r = send(
        nginx_url,
        "POST",
        path,
        headers=target["headers"],
        files={"file": ("bundle.json", b" " * (40 * MIB), "application/json")},
        data={"course_id": str(target["course"])},
    )
    assert reached_backend(r), (r.status_code, r.text[:200])
    assert r.status_code == 422, r.text
    r = send(
        nginx_url,
        "POST",
        path,
        headers=target["headers"],
        files={"file": ("bundle.json", b" " * (42 * MIB), "application/json")},
        data={"course_id": str(target["course"])},
    )
    assert refused_by_nginx(r), (r.status_code, r.text[:200])
