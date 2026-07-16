import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ANPR_DB", str(tmp_path / "test.db"))
    monkeypatch.delenv("ANPR_DEMO", raising=False)
    from anpr.main import app

    with TestClient(app) as test_client:
        yield test_client


def test_index_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Dahua ANPR Monitor" in resp.text


def test_camera_crud(client):
    resp = client.post("/api/cameras", json={
        "name": "Gate 1", "host": "192.0.2.10", "port": 80,
        "username": "admin", "password": "secret", "enabled": False,
    })
    assert resp.status_code == 201
    cam = resp.json()
    assert cam["name"] == "Gate 1"
    assert "password" not in cam

    resp = client.get("/api/cameras")
    assert len(resp.json()) == 1

    resp = client.put(f"/api/cameras/{cam['id']}", json={"name": "Gate A"})
    assert resp.json()["name"] == "Gate A"

    # Empty password on update means "keep existing".
    client.put(f"/api/cameras/{cam['id']}", json={"password": ""})

    resp = client.delete(f"/api/cameras/{cam['id']}")
    assert resp.status_code == 204
    assert client.get("/api/cameras").json() == []


def test_event_search_and_export(client):
    from anpr.models import AnprEvent

    db = client.app.state.db
    for plate in ("ABC123", "ABC999", "XYZ777"):
        db.add_event(AnprEvent(
            camera_id=1, camera_name="Gate 1", plate=plate,
            received_at="2026-07-15T10:00:00+02:00",
        ))

    resp = client.get("/api/events", params={"plate": "ABC"})
    data = resp.json()
    assert data["total"] == 2
    assert all("ABC" in e["plate"] for e in data["events"])
    assert "image_b64" not in data["events"][0]

    resp = client.get("/api/events", params={"date_from": "2026-07-16"})
    assert resp.json()["total"] == 0
    resp = client.get("/api/events",
                      params={"date_from": "2026-07-15", "date_to": "2026-07-15"})
    assert resp.json()["total"] == 3

    resp = client.get("/api/events/export.csv", params={"plate": "XYZ"})
    assert resp.status_code == 200
    assert "XYZ777" in resp.text
    assert "ABC123" not in resp.text


def test_event_image_endpoint(client):
    import base64

    from anpr.models import AnprEvent

    db = client.app.state.db
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 10
    event_id = db.add_event(AnprEvent(
        camera_id=1, camera_name="Gate 1", plate="IMG001",
        received_at="2026-07-15T10:00:00+02:00",
        image_b64=base64.b64encode(jpeg).decode(),
    ))
    resp = client.get(f"/api/events/{event_id}/image")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content == jpeg

    assert client.get("/api/events/99999/image").status_code == 404


def test_report_settings_roundtrip(client, tmp_path):
    resp = client.get("/api/settings/report")
    assert resp.json()["enabled"] is False

    resp = client.put("/api/settings/report", json={
        "enabled": True, "time": "06:30", "directory": str(tmp_path / "out"),
    })
    assert resp.status_code == 200
    assert client.get("/api/settings/report").json()["time"] == "06:30"

    resp = client.post("/api/reports/run")
    assert resp.status_code == 200
    assert os.path.exists(resp.json()["path"])


def test_sync_history_unreachable(client):
    resp = client.post("/api/cameras", json={
        "name": "Gate 1", "host": "192.0.2.1", "port": 81,
        "username": "admin", "password": "x", "enabled": False,
    })
    camera_id = resp.json()["id"]
    resp = client.post(f"/api/cameras/{camera_id}/sync")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False

    assert client.post("/api/cameras/99999/sync").status_code == 404


def test_whitelist_crud_and_matching(client):
    from anpr.database import normalize_plate

    resp = client.post("/api/whitelist", json={"plate": "AB-12 34", "label": "Boss"})
    assert resp.status_code == 201
    entry = resp.json()
    assert entry["label"] == "Boss"

    # Duplicate (normalised) plate updates rather than duplicating.
    client.post("/api/whitelist", json={"plate": "ab1234", "label": "same car"})
    listed = client.get("/api/whitelist").json()
    assert len(listed) == 1

    db = client.app.state.db
    assert db.whitelist_contains("AB1234") is True
    assert db.whitelist_contains("a b 1 2 3 4") is True   # normalisation
    assert db.whitelist_contains("ZZ9999") is False
    assert normalize_plate("ab-12 34") == "AB1234"

    resp = client.delete(f"/api/whitelist/{entry['id']}")
    assert resp.status_code == 204
    assert client.get("/api/whitelist").json() == []


def test_access_settings_roundtrip_keeps_password(client):
    resp = client.put("/api/settings/access", json={
        "enabled": True, "email_enabled": True, "smtp_host": "smtp.example.com",
        "smtp_user": "u", "smtp_password": "secret", "email_to": "a@b.com",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["smtp_password"] == ""          # never returned
    assert body["smtp_password_set"] is True

    # Saving again without a password keeps the stored one.
    client.put("/api/settings/access", json={
        "enabled": True, "email_enabled": True, "smtp_host": "smtp.example.com",
        "smtp_user": "u", "smtp_password": "", "email_to": "a@b.com",
    })
    assert client.app.state.access.get_settings().smtp_password == "secret"


def test_retention_purge(client):
    import base64

    from anpr.models import AnprEvent

    db = client.app.state.db
    jpeg = base64.b64encode(b"\xff\xd8\xff\xe0abc").decode()
    old = db.add_event(AnprEvent(camera_id=1, plate="OLD001",
                                 received_at="2020-01-01T10:00:00+00:00",
                                 image_b64=jpeg))
    new = db.add_event(AnprEvent(camera_id=1, plate="NEW001",
                                 received_at="2999-01-01T10:00:00+00:00",
                                 image_b64=jpeg))
    # clear-image mode
    cleared = db.purge_old_images("2025-01-01T00:00:00+00:00")
    assert cleared == 1
    assert db.get_event_image(old) is None
    assert db.get_event_image(new) is not None
    # delete-record mode
    deleted = db.purge_old_events("2025-01-01T00:00:00+00:00")
    assert deleted == 1
    assert db.search_events(plate="OLD001")["total"] == 0
    assert db.search_events(plate="NEW001")["total"] == 1


def test_retention_settings_api(client):
    resp = client.put("/api/settings/retention",
                      json={"enabled": True, "days": 7, "delete_records": False})
    assert resp.status_code == 200
    assert client.get("/api/settings/retention").json()["days"] == 7
    assert client.post("/api/retention/run").json()["ok"] is True


def test_diagnostics(client):
    resp = client.get("/api/diagnostics")
    data = resp.json()
    assert "database_path" in data
    assert data["cameras"] == 0
    assert data["demo_mode"] is False


def test_connection_test_unreachable(client):
    resp = client.post("/api/cameras/test", json={
        "host": "192.0.2.1", "port": 81, "username": "admin", "password": "x",
    })
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
