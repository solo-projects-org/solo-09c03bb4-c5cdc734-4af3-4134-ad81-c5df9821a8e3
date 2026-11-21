import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def post_manuscript(client, text):
    return client.post("/api/manuscripts", json={"text": text})


def annotate(client, version, start, end, label):
    return client.post(
        "/api/annotations",
        json={"version": version, "start": start, "end": end, "label": label},
    )


def replace(client, version, text):
    return client.post("/api/versions", json={"version": version, "text": text})
