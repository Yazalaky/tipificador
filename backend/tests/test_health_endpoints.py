import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[3]),
)

import backend.app.main as main


class FakeBlob:
    def __init__(self, accessible=True):
        self.accessible = accessible

    def exists(self):
        if not self.accessible:
            raise RuntimeError("GCS unavailable")

        return False


class FakeBucket:
    def __init__(self, accessible=True):
        self.accessible = accessible

    def blob(self, name):
        return FakeBlob(self.accessible)


class FakeStorageClient:
    def __init__(self, accessible=True):
        self.accessible = accessible

    def bucket(self, name):
        return FakeBucket(self.accessible)


def configure_ready_environment(
    monkeypatch,
    tmp_path,
    *,
    gcs_accessible=True,
):
    job_root = tmp_path / "jobs"
    batch_root = tmp_path / "batches"

    job_root.mkdir()
    batch_root.mkdir()

    monkeypatch.setattr(main, "JOB_ROOT", str(job_root))
    monkeypatch.setattr(main, "BATCH_ROOT", str(batch_root))
    monkeypatch.setattr(main, "OCR_ENABLED", False)

    monkeypatch.setattr(
        main,
        "GCS_BUCKET",
        "test-bucket",
    )

    monkeypatch.setattr(
        main,
        "GCS_RESULTS_PREFIX",
        "results/",
    )

    monkeypatch.setattr(
        main,
        "_gcs_client",
        lambda: FakeStorageClient(gcs_accessible),
    )

    monkeypatch.setenv(
        "K_REVISION",
        "tipificador-api-test-revision",
    )


def test_health_live():
    client = TestClient(main.app)

    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_health_ready_success(monkeypatch, tmp_path):
    configure_ready_environment(
        monkeypatch,
        tmp_path,
        gcs_accessible=True,
    )

    client = TestClient(main.app)
    response = client.get("/health/ready")

    assert response.status_code == 200

    body = response.json()

    assert body["status"] == "ready"
    assert body["storage"] == "ok"
    assert body["version"] == "tipificador-api-test-revision"
    assert all(body["checks"].values())


def test_health_ready_returns_503_when_gcs_fails(
    monkeypatch,
    tmp_path,
):
    configure_ready_environment(
        monkeypatch,
        tmp_path,
        gcs_accessible=False,
    )

    client = TestClient(main.app)
    response = client.get("/health/ready")

    assert response.status_code == 503

    body = response.json()["detail"]

    assert body["status"] == "not_ready"
    assert body["storage"] == "unavailable"
    assert body["checks"]["gcs_configured"] is True
    assert body["checks"]["gcs_accessible"] is False
