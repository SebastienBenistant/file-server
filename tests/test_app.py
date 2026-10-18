from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app import Settings, create_app, load_settings


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "welcome.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "folder").mkdir()
    return tmp_path


def make_client(root: Path, *, token: str | None = None, limit: int = 1024) -> TestClient:
    return TestClient(create_app(Settings(roots={"files": root.resolve()}, token=token, max_upload_bytes=limit)))


def test_root_browse_and_download(root: Path) -> None:
    client = make_client(root)

    roots_response = client.get("/api/roots")
    assert roots_response.status_code == 200
    assert roots_response.json() == {"roots": [{"id": "files", "name": "files"}]}

    entries_response = client.get("/api/roots/files/entries")
    assert entries_response.status_code == 200
    assert [entry["name"] for entry in entries_response.json()["entries"]] == ["folder", "welcome.txt"]

    download_response = client.get("/api/roots/files/download", params={"path": "welcome.txt"})
    assert download_response.status_code == 200
    assert download_response.content == b"hello"
    assert "welcome.txt" in download_response.headers["content-disposition"]
    assert client.get("/api/roots/files/download", params={"path": ""}).status_code == 400


def test_multiple_roots_are_browsed_independently(root: Path, tmp_path: Path) -> None:
    second_root = tmp_path / "second"
    second_root.mkdir()
    (second_root / "second-only.txt").write_text("two", encoding="utf-8")
    client = TestClient(
        create_app(
            Settings(
                roots={"first": root.resolve(), "second": second_root.resolve()},
            )
        )
    )

    roots_response = client.get("/api/roots")
    assert roots_response.json() == {
        "roots": [
            {"id": "first", "name": "first"},
            {"id": "second", "name": "second"},
        ]
    }
    first_names = {entry["name"] for entry in client.get("/api/roots/first/entries").json()["entries"]}
    second_names = {entry["name"] for entry in client.get("/api/roots/second/entries").json()["entries"]}
    assert "second-only.txt" not in first_names
    assert second_names == {"second-only.txt"}


def test_ui_and_html_directory_listing_render(root: Path) -> None:
    client = make_client(root)
    page = client.get("/")
    assert page.status_code == 200
    assert "File Server" in page.text

    listing = client.get("/ui/entries", params={"root_id": "files", "path": ""})
    assert listing.status_code == 200
    assert "welcome.txt" in listing.text
    assert "folder" in listing.text

    nested = client.get("/ui/entries", params={"root_id": "files", "path": "folder"})
    assert nested.status_code == 200
    assert 'data-path="folder"' in nested.text


def test_authentication_is_optional_or_required(root: Path) -> None:
    protected = make_client(root, token="secret")
    assert protected.get("/api/auth/status").json() == {"authentication_required": True}
    assert protected.get("/api/roots").status_code == 401
    assert protected.get("/api/roots", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert protected.get("/api/roots", headers={"Authorization": "Bearer secret"}).status_code == 200

    unprotected = make_client(root)
    assert unprotected.get("/api/auth/status").json() == {"authentication_required": False}
    assert unprotected.get("/api/roots").status_code == 200


def test_create_directory_and_upload_without_overwrite(root: Path) -> None:
    client = make_client(root)
    created = client.post(
        "/api/roots/files/directories",
        json={"path": "", "name": "new-folder"},
    )
    assert created.status_code == 201
    assert (root / "new-folder").is_dir()
    assert client.post(
        "/api/roots/files/directories",
        json={"path": "", "name": "new-folder"},
    ).status_code == 409

    uploaded = client.post(
        "/api/roots/files/uploads",
        files=[("files", ("new.txt", b"content", "text/plain"))],
    )
    assert uploaded.status_code == 201
    assert (root / "new.txt").read_bytes() == b"content"

    nested_upload = client.post(
        "/api/roots/files/uploads",
        params={"path": "folder"},
        files=[("files", ("inside.txt", b"nested", "text/plain"))],
    )
    assert nested_upload.status_code == 201
    assert (root / "folder" / "inside.txt").read_bytes() == b"nested"

    conflict = client.post(
        "/api/roots/files/uploads",
        files=[("files", ("new.txt", b"replace", "text/plain"))],
    )
    assert conflict.status_code == 409
    assert (root / "new.txt").read_bytes() == b"content"


def test_invalid_and_outside_paths_are_rejected(root: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    client = make_client(root)

    for invalid in ("../outside.txt", str(outside), "folder/../welcome.txt", r"..\outside.txt"):
        response = client.get("/api/roots/files/entries", params={"path": invalid})
        assert response.status_code in {400, 403}
        assert str(outside) not in response.text

    assert client.get("/api/roots/missing/entries").status_code == 404


def test_upload_limit_rejects_and_does_not_leave_partial_file(root: Path) -> None:
    client = make_client(root, limit=3)
    response = client.post(
        "/api/roots/files/uploads",
        files=[("files", ("large.txt", b"four", "text/plain"))],
    )
    assert response.status_code == 413
    assert not (root / "large.txt").exists()
    assert not list(root.glob(".file-server-upload-*"))


def test_batch_upload_is_rolled_back_when_a_later_name_conflicts(root: Path) -> None:
    (root / "exists.txt").write_text("original", encoding="utf-8")
    client = make_client(root)
    response = client.post(
        "/api/roots/files/uploads",
        files=[
            ("files", ("first.txt", b"first", "text/plain")),
            ("files", ("exists.txt", b"replace", "text/plain")),
        ],
    )
    assert response.status_code == 409
    assert not (root / "first.txt").exists()
    assert (root / "exists.txt").read_text(encoding="utf-8") == "original"


def test_symlinks_outside_root_are_not_listed_or_accessible(root: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-directory"
    outside.mkdir()
    (outside / "outside-secret.txt").write_text("secret", encoding="utf-8")
    link = root / "external-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        if sys.platform != "win32":
            pytest.skip("Symlink creation is not available")
        junction = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            check=False,
            text=True,
        )
        if junction.returncode != 0:
            pytest.skip("Neither symlinks nor junctions are available")

    client = make_client(root)
    names = [entry["name"] for entry in client.get("/api/roots/files/entries").json()["entries"]]
    assert "external-link" not in names
    assert client.get(
        "/api/roots/files/entries",
        params={"path": "external-link"},
    ).status_code == 403
    assert client.get(
        "/api/roots/files/download",
        params={"path": "external-link/outside-secret.txt"},
    ).status_code == 403


def test_settings_parse_environment_roots_and_limit(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "FILE_SERVER_ROOTS": json.dumps({"docs": str(tmp_path)}),
            "FILE_SERVER_MAX_UPLOAD_BYTES": "25",
            "FILE_SERVER_TOKEN": "token",
        }
    )
    assert settings.roots["docs"] == tmp_path.resolve()
    assert settings.max_upload_bytes == 25
    assert settings.token == "token"


def test_invalid_settings_are_explicit() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        load_settings({"FILE_SERVER_MAX_UPLOAD_BYTES": "0"})
    with pytest.raises(ValueError, match="JSON object"):
        load_settings({"FILE_SERVER_ROOTS": "[]"})
