"""The reading UI (M3): served by the app, same origin, built only on the public /api routes."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from slidetwin.api.main import WEB_DIR

JS_FILES = sorted(WEB_DIR.glob("*.js"))
WEB_FILES = sorted(p for p in WEB_DIR.iterdir() if p.suffix in {".js", ".html", ".css"})


def test_index_and_static_files_are_served(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["cache-control"] == "no-cache"
    assert '<script type="module" src="/static/app.js"></script>' in resp.text
    assert "Content-Security-Policy" in resp.text
    for name, mime in [("app.js", "text/javascript"), ("styles.css", "text/css")]:
        resp = client.get(f"/static/{name}")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith(mime)
        assert resp.headers["cache-control"] == "no-cache"  # a redeploy reaches every reader
        again = client.get(f"/static/{name}", headers={"if-none-match": resp.headers["etag"]})
        assert again.status_code == 304


def test_static_mount_serves_only_the_web_directory(client: TestClient) -> None:
    for path in ["/static/../api/main.py", "/static/%2e%2e/api/main.py", "/static/nope.js"]:
        assert client.get(path).status_code == 404
    paths = client.get("/openapi.json").json()["paths"]
    assert all(p.startswith("/api/") for p in paths)


def test_ui_talks_only_to_the_api() -> None:
    assert {p.name for p in JS_FILES} >= {"api.js", "app.js", "geometry.js", "i18n.js", "report.js"}
    for path in WEB_FILES:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"https?://", text), f"{path.name} loads an external resource"
    for path in JS_FILES:
        text = path.read_text(encoding="utf-8")
        assert ("fetch(" in text) == (path.name == "api.js"), path.name
        for literal in re.findall(r"[\"'`](/[^\"'`]*)", text):
            assert literal.startswith("/api/"), f"{path.name}: {literal}"
            assert "tile" not in literal.lower(), f"{path.name}: {literal}"
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)="([^"]*)"', html)
    assert refs
    assert all(r.startswith("/static/") or r == "#" for r in refs), refs


def test_ui_shows_the_research_banner() -> None:
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    i18n = (WEB_DIR / "i18n.js").read_text(encoding="utf-8")
    assert 'data-i18n="banner.research"' in html
    assert "Not for clinical diagnosis" in i18n
    assert "不用于临床诊断" in i18n


def test_web_dir_is_inside_the_package() -> None:
    import slidetwin

    assert WEB_DIR.is_relative_to(Path(slidetwin.__file__).resolve().parent)
