import json
import shutil
import sys
from pathlib import Path

import pytest
from google.api_core import exceptions as google_exceptions

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import backend.app.main as main


class FakePreconditionFailed(getattr(google_exceptions, "PreconditionFailed", Exception)):  # type: ignore[reportGeneralTypeIssues]
    code = 412


class FakeBlob:
    def __init__(self, store, name):
        self.store = store
        self.name = name
        self.generation = None

    def reload(self):
        if self.name not in self.store.objects:
            raise FileNotFoundError(self.name)
        self.generation = self.store.objects[self.name]["generation"]
        return self

    def upload_from_string(self, data, content_type=None, if_generation_match=None):
        self.store.upload_attempts.append((self.name, if_generation_match, data))
        if self.store.next_upload_error is not None:
            error = self.store.next_upload_error
            self.store.next_upload_error = None
            raise error

        if self.store.fail_uploads_left > 0:
            self.store.fail_uploads_left -= 1
            raise RuntimeError("temporary gcs failure")

        current = self.store.objects.get(self.name)
        current_generation = current["generation"] if current else 0

        if self.store.force_conflict_once and not self.store.conflict_triggered:
            self.store.conflict_triggered = True
            if current is not None:
                self.store.objects[self.name] = {
                    "data": current["data"],
                    "generation": current_generation + 1,
                }
            else:
                self.store.objects[self.name] = {"data": data, "generation": 1}
            raise FakePreconditionFailed("precondition failed")

        if if_generation_match is not None and if_generation_match != current_generation:
            raise FakePreconditionFailed("precondition failed")

        new_generation = current_generation + 1
        self.store.objects[self.name] = {"data": data, "generation": new_generation}
        self.generation = new_generation

    def download_as_text(self, encoding="utf-8"):
        if self.name not in self.store.objects:
            raise FileNotFoundError(self.name)
        if self.store.verify_mismatch and self.name == self.store.verify_target:
            return self.store.verify_payload
        return self.store.objects[self.name]["data"]

    def exists(self):
        return self.name in self.store.objects

    def delete(self):
        self.store.objects.pop(self.name, None)

    def upload_from_filename(self, filename, content_type=None):
        with open(filename, "rb") as f:
            data = f.read().decode("latin1")
        self.upload_from_string(data, content_type=content_type, if_generation_match=None)


class FakeBucket:
    def __init__(self, store):
        self.store = store

    def blob(self, name):
        return FakeBlob(self.store, name)


class FakeStorageClient:
    def __init__(self, store):
        self.store = store

    def bucket(self, name):
        self.store.last_bucket = name
        return FakeBucket(self.store)


class FakeGCSStore:
    def __init__(self):
        self.objects = {}
        self.upload_attempts = []
        self.fail_uploads_left = 0
        self.force_conflict_once = False
        self.conflict_triggered = False
        self.last_bucket = None
        self.verify_mismatch = False
        self.verify_target = None
        self.verify_payload = ""
        self.next_upload_error = None


@pytest.fixture()
def isolated_batch_fs(tmp_path, monkeypatch):
    batch_root = tmp_path / "batches"
    batch_root.mkdir()
    monkeypatch.setattr(main, "JOB_ROOT", str(tmp_path))
    monkeypatch.setattr(main, "BATCH_ROOT", str(batch_root))
    return tmp_path


@pytest.fixture()
def fake_gcs(monkeypatch):
    store = FakeGCSStore()
    monkeypatch.setattr(main, "GCS_BUCKET", "tipificador-zips-prod")
    monkeypatch.setattr(main, "_gcs_enabled", lambda: True)
    monkeypatch.setattr(main, "_gcs_client", lambda: FakeStorageClient(store))
    monkeypatch.setattr(main.time, "sleep", lambda *_args, **_kwargs: None)
    return store


def _write_meta(batch_dir: Path, meta: dict) -> Path:
    batch_dir.mkdir(parents=True, exist_ok=True)
    path = batch_dir / "meta.json"
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_initial_creation_uses_if_generation_match_zero(isolated_batch_fs, fake_gcs):
    batch_id = "a" * 32
    batch_dir = Path(main._batch_dir(batch_id))
    _write_meta(batch_dir, {"batchId": batch_id, "status": "processing", "metaRevision": 0})

    saved = main._save_batch_meta(batch_id, main._load_batch_meta_latest(batch_id))

    assert fake_gcs.upload_attempts[0][1] == 0
    assert saved["metaRevision"] == 1
    assert json.loads(fake_gcs.objects[main._batch_meta_object_name(batch_id)]["data"])["metaRevision"] == 1


def test_transient_failure_retries_and_succeeds(isolated_batch_fs, fake_gcs):
    batch_id = "b" * 32
    batch_dir = Path(main._batch_dir(batch_id))
    _write_meta(batch_dir, {"batchId": batch_id, "status": "processing", "metaRevision": 0})
    fake_gcs.fail_uploads_left = 1

    saved = main._save_batch_meta(batch_id, main._load_batch_meta_latest(batch_id), final=True)

    assert len(fake_gcs.upload_attempts) >= 2
    assert saved["metaRevision"] == 1
    assert json.loads(fake_gcs.objects[main._batch_meta_object_name(batch_id)]["data"]) == saved


def test_permanent_final_persistence_failure_raises(isolated_batch_fs, fake_gcs):
    batch_id = "c" * 32
    batch_dir = Path(main._batch_dir(batch_id))
    _write_meta(batch_dir, {"batchId": batch_id, "status": "processing", "metaRevision": 0})
    fake_gcs.fail_uploads_left = 99

    with pytest.raises(main.BatchMetaPersistenceError, match="No se pudo persistir la metadata final"):
        main._save_batch_meta(batch_id, main._load_batch_meta_latest(batch_id), final=True)


def test_precondition_conflict_reloads_combines_and_retries(isolated_batch_fs, fake_gcs):
    batch_id = "d" * 32
    batch_dir = Path(main._batch_dir(batch_id))
    _write_meta(
        batch_dir,
        {
            "batchId": batch_id,
            "status": "processing",
            "metaRevision": 1,
            "packages": [
                {"name": "P1", "status": "processing", "startedAt": 1.0, "lastHeartbeatAt": 2.0},
            ],
        },
    )
    # Remote writer already advanced and finished the batch.
    fake_gcs.objects[main._batch_meta_object_name(batch_id)] = {
        "data": json.dumps(
            {
                "batchId": batch_id,
                "status": "done",
                "metaRevision": 2,
                "finishedAt": 10.0,
                "gcsResult": "gs://tipificador-zips-prod/results/d/P1.zip",
                "gcsAllZip": "gs://tipificador-zips-prod/results/d/all.zip",
                "packages": [
                    {"name": "P1", "status": "done", "finishedAt": 10.0, "resultFile": "P1.zip"},
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        "generation": 2,
    }
    fake_gcs.force_conflict_once = True

    local = main._load_batch_meta_latest(batch_id)
    local["status"] = "processing"
    local["gcsResult"] = None
    local["gcsAllZip"] = None
    local["packages"][0]["status"] = "processing"

    saved = main._save_batch_meta(batch_id, local, final=True)

    assert saved["status"] == "done"
    assert saved["gcsResult"] == "gs://tipificador-zips-prod/results/d/P1.zip"
    assert saved["gcsAllZip"] == "gs://tipificador-zips-prod/results/d/all.zip"
    assert saved["packages"][0]["status"] == "done"
    assert saved["metaRevision"] == 4


def test_nonfinal_save_after_precondition_conflict_persists_merged_remote_state(isolated_batch_fs, fake_gcs):
    batch_id = "4" * 32
    batch_dir = Path(main._batch_dir(batch_id))
    _write_meta(
        batch_dir,
        {
            "batchId": batch_id,
            "status": "processing",
            "metaRevision": 1,
            "gcsResult": None,
            "gcsAllZip": None,
            "packages": [{"name": "P1", "status": "processing"}],
        },
    )
    fake_gcs.objects[main._batch_meta_object_name(batch_id)] = {
        "data": json.dumps(
            {
                "batchId": batch_id,
                "status": "done",
                "metaRevision": 2,
                "finishedAt": 11.0,
                "gcsResult": "gs://tipificador-zips-prod/results/4/P1.zip",
                "gcsAllZip": "gs://tipificador-zips-prod/results/4/all.zip",
                "packages": [{"name": "P1", "status": "done", "resultFile": "P1.zip"}],
            },
            ensure_ascii=False,
            indent=2,
        ),
        "generation": 2,
    }
    fake_gcs.force_conflict_once = True

    meta = main._load_batch_meta_latest(batch_id)
    meta["status"] = "processing"
    meta["gcsResult"] = None
    meta["gcsAllZip"] = None
    meta["packages"][0]["status"] = "processing"

    saved = main._save_batch_meta(batch_id, meta)
    persisted = json.loads((batch_dir / "meta.json").read_text(encoding="utf-8"))

    assert saved == persisted
    assert persisted["status"] == "done"
    assert persisted["gcsResult"] == "gs://tipificador-zips-prod/results/4/P1.zip"
    assert persisted["gcsAllZip"] == "gs://tipificador-zips-prod/results/4/all.zip"
    assert persisted["packages"][0]["status"] == "done"
    assert persisted["metaRevision"] == 4
    assert persisted["metaSyncError"].startswith("No se pudo persistir metadata del batch")
    assert fake_gcs.upload_attempts[0][1] == 2


def test_save_batch_meta_to_gcs_precondition_branch_reloads_and_merges(isolated_batch_fs, fake_gcs):
    batch_id = "5" * 32
    batch_dir = Path(main._batch_dir(batch_id))
    _write_meta(
        batch_dir,
        {
            "batchId": batch_id,
            "status": "processing",
            "metaRevision": 1,
            "gcsResult": None,
            "gcsAllZip": None,
            "packages": [{"name": "P1", "status": "processing"}],
        },
    )
    fake_gcs.objects[main._batch_meta_object_name(batch_id)] = {
        "data": json.dumps(
            {
                "batchId": batch_id,
                "status": "done",
                "metaRevision": 2,
                "finishedAt": 11.0,
                "gcsResult": "gs://tipificador-zips-prod/results/5/P1.zip",
                "gcsAllZip": "gs://tipificador-zips-prod/results/5/all.zip",
                "packages": [{"name": "P1", "status": "done", "resultFile": "P1.zip"}],
            },
            ensure_ascii=False,
            indent=2,
        ),
        "generation": 2,
    }
    fake_gcs.force_conflict_once = True

    result = main._save_batch_meta_to_gcs(batch_id, main._load_batch_meta_latest(batch_id))

    assert not result.success
    assert result.error_kind == "precondition"
    assert isinstance(result.error, FakePreconditionFailed)
    assert result.observed_generation == 3
    assert result.final_meta["status"] == "done"
    assert result.final_meta["gcsResult"] == "gs://tipificador-zips-prod/results/5/P1.zip"
    assert result.final_meta["gcsAllZip"] == "gs://tipificador-zips-prod/results/5/all.zip"
    assert result.final_meta["packages"][0]["status"] == "done"


def test_save_batch_meta_to_gcs_transient_error_is_not_conflict(isolated_batch_fs, fake_gcs):
    batch_id = "6" * 32
    batch_dir = Path(main._batch_dir(batch_id))
    _write_meta(batch_dir, {"batchId": batch_id, "status": "processing", "metaRevision": 0})
    fake_gcs.fail_uploads_left = 1

    result = main._save_batch_meta_to_gcs(batch_id, main._load_batch_meta_latest(batch_id))

    assert not result.success
    assert result.error_kind == "transient"
    assert not isinstance(result.error, FakePreconditionFailed)
    assert result.observed_generation == 0


def test_save_batch_meta_to_gcs_permanent_error_is_not_conflict(isolated_batch_fs, fake_gcs):
    batch_id = "7" * 32
    batch_dir = Path(main._batch_dir(batch_id))
    _write_meta(batch_dir, {"batchId": batch_id, "status": "processing", "metaRevision": 0})
    fake_gcs.next_upload_error = google_exceptions.Forbidden("boom")

    result = main._save_batch_meta_to_gcs(batch_id, main._load_batch_meta_latest(batch_id))

    assert not result.success
    assert result.error_kind == "permanent"
    assert not isinstance(result.error, FakePreconditionFailed)
    assert len(fake_gcs.upload_attempts) == 1


def test_final_save_verifies_remote_and_fails_on_mismatch(isolated_batch_fs, fake_gcs):
    batch_id = "8" * 32
    batch_dir = Path(main._batch_dir(batch_id))
    _write_meta(batch_dir, {"batchId": batch_id, "status": "processing", "metaRevision": 0})
    meta = main._load_batch_meta_latest(batch_id)
    meta["status"] = "done"
    meta["finishedAt"] = 10.5
    meta["elapsedSeconds"] = 2.5
    meta["gcsResult"] = "gs://tipificador-zips-prod/results/8/P1.zip"
    meta["gcsAllZip"] = "gs://tipificador-zips-prod/results/8/all.zip"
    fake_gcs.verify_mismatch = True
    fake_gcs.verify_target = main._batch_meta_object_name(batch_id)
    fake_gcs.verify_payload = json.dumps({
        "batchId": batch_id,
        "status": "processing",
        "metaRevision": 1,
    }, ensure_ascii=False)

    with pytest.raises(main.BatchMetaVerificationError, match="Verificación final de metadata falló"):
        main._save_batch_meta(batch_id, meta, final=True)

    persisted = json.loads((batch_dir / "meta.json").read_text(encoding="utf-8"))
    assert persisted["status"] == "done"
    assert persisted["gcsResult"] == meta["gcsResult"]
    assert persisted["gcsAllZip"] == meta["gcsAllZip"]
    assert meta["metaSyncError"].startswith("Verificación final de metadata falló")


def test_process_batch_emits_finalization_error_when_verification_fails(isolated_batch_fs, fake_gcs, monkeypatch):
    batch_id = "9" * 32
    batch_dir = Path(main._batch_dir(batch_id))
    results_dir = batch_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    _write_meta(
        batch_dir,
        {
            "batchId": batch_id,
            "status": "pending",
            "metaRevision": 0,
            "packages": [{"name": "P1", "status": "pending"}],
        },
    )

    def fake_worker(*_args, **_kwargs):
        result_path = results_dir / "P1.zip"
        result_path.write_text("zip", encoding="utf-8")
        return {"jobId": "job-1", "resultFile": "P1.zip", "downloadName": "P1.zip"}

    def fake_zip(_batch_id, meta):
        all_path = results_dir / "all.zip"
        all_path.write_text("all", encoding="utf-8")
        meta["allZip"] = "all.zip"
        return str(all_path)

    stages = []

    def fake_log_timing(event, **fields):
        if event == "batch_timing":
            stages.append(fields.get("stage"))

    monkeypatch.setattr(main, "_run_batch_package_worker", fake_worker)
    monkeypatch.setattr(main, "_build_consolidated_batch_zip", fake_zip)
    monkeypatch.setattr(main, "_log_timing", fake_log_timing)
    monkeypatch.setattr(main, "_gcs_enabled", lambda: True)
    monkeypatch.setattr(main, "_gcs_client", lambda: FakeStorageClient(fake_gcs))
    monkeypatch.setattr(main.time, "sleep", lambda *_args, **_kwargs: None)

    fake_gcs.verify_mismatch = True
    fake_gcs.verify_target = main._batch_meta_object_name(batch_id)
    fake_gcs.verify_payload = json.dumps({
        "batchId": batch_id,
        "status": "processing",
        "metaRevision": 1,
    }, ensure_ascii=False)

    main._process_batch(batch_id)

    assert "batch_finalization_error" in stages
    assert "batch_done" not in stages
    disk_meta = json.loads((batch_dir / "meta.json").read_text(encoding="utf-8"))
    assert disk_meta["metaSyncError"].startswith("Verificación final de metadata falló")


@pytest.mark.parametrize(
    ("local_status", "remote_status"),
    [
        ("processing", "cancelling"),
        ("cancelling", "processing"),
        ("pending", "cancelling"),
    ],
    ids=[
        "remote-cancelling-local-processing",
        "local-cancelling-remote-processing",
        "remote-cancelling-local-pending",
    ],
)
def test_merge_batch_meta_prefers_cancelling_over_nonterminal_states(local_status, remote_status):
    batch_id = "a" * 32
    local = {
        "batchId": batch_id,
        "status": local_status,
        "metaRevision": 3,
        "cancelRequested": local_status == "cancelling",
        "packages": [{"name": "P1", "status": local_status if local_status != "cancelling" else "processing"}],
    }
    remote = {
        "batchId": batch_id,
        "status": remote_status,
        "metaRevision": 4,
        "cancelRequested": remote_status == "cancelling",
        "packages": [{"name": "P1", "status": remote_status if remote_status != "cancelling" else "processing"}],
    }

    merged = main._merge_batch_meta(local, remote)

    assert merged["status"] == "cancelling"
    assert merged["cancelRequested"] is True


def test_merge_batch_meta_keeps_remote_terminal_over_local_cancelling():
    batch_id = "b" * 32
    local = {
        "batchId": batch_id,
        "status": "cancelling",
        "metaRevision": 3,
        "cancelRequested": True,
        "packages": [{"name": "P1", "status": "processing"}],
    }
    remote = {
        "batchId": batch_id,
        "status": "done",
        "metaRevision": 4,
        "cancelRequested": False,
        "finishedAt": 22.0,
        "elapsedSeconds": 11.0,
        "gcsResult": "gs://tipificador-zips-prod/results/b/P1.zip",
        "gcsAllZip": "gs://tipificador-zips-prod/results/b/all.zip",
        "packages": [
            {"name": "P1", "status": "done", "finishedAt": 22.0, "resultFile": "P1.zip"},
        ],
    }

    merged = main._merge_batch_meta(local, remote)

    assert merged["status"] == "done"
    assert merged["cancelRequested"] is True
    assert merged["finishedAt"] == 22.0
    assert merged["elapsedSeconds"] == 11.0
    assert merged["gcsResult"] == "gs://tipificador-zips-prod/results/b/P1.zip"
    assert merged["gcsAllZip"] == "gs://tipificador-zips-prod/results/b/all.zip"
    assert merged["packages"][0]["status"] == "done"
    assert merged["packages"][0]["finishedAt"] == 22.0


def test_merge_batch_meta_keeps_local_terminal_over_remote_cancelling():
    batch_id = "c" * 32
    local = {
        "batchId": batch_id,
        "status": "done",
        "metaRevision": 3,
        "cancelRequested": False,
        "finishedAt": 19.0,
        "elapsedSeconds": 9.0,
        "gcsResult": "gs://tipificador-zips-prod/results/c/P1.zip",
        "gcsAllZip": "gs://tipificador-zips-prod/results/c/all.zip",
        "packages": [
            {"name": "P1", "status": "done", "finishedAt": 19.0, "resultFile": "P1.zip"},
        ],
    }
    remote = {
        "batchId": batch_id,
        "status": "cancelling",
        "metaRevision": 4,
        "cancelRequested": True,
        "packages": [{"name": "P1", "status": "processing"}],
    }

    merged = main._merge_batch_meta(local, remote)

    assert merged["status"] == "done"
    assert merged["cancelRequested"] is True
    assert merged["finishedAt"] == 19.0
    assert merged["elapsedSeconds"] == 9.0
    assert merged["gcsResult"] == "gs://tipificador-zips-prod/results/c/P1.zip"
    assert merged["gcsAllZip"] == "gs://tipificador-zips-prod/results/c/all.zip"
    assert merged["packages"][0]["status"] == "done"
    assert merged["packages"][0]["finishedAt"] == 19.0


def test_reconcile_batch_meta_keeps_cancelling_when_pending_packages_remain(isolated_batch_fs):
    batch_id = "d" * 32
    batch_dir = Path(main._batch_dir(batch_id))
    results_dir = batch_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "all.zip").write_text("all", encoding="utf-8")
    _write_meta(
        batch_dir,
        {
            "batchId": batch_id,
            "status": "cancelling",
            "metaRevision": 2,
            "cancelRequested": True,
            "allZip": "all.zip",
            "packages": [
                {"name": "P1", "status": "pending", "startedAt": 1.0, "lastHeartbeatAt": 1.0},
            ],
        },
    )

    reconciled = main._reconcile_batch_meta(batch_id, main._load_batch_meta_latest(batch_id), persist=False)

    assert reconciled["status"] == "cancelling"
    assert reconciled["cancelRequested"] is True
    assert reconciled["allZip"] == "all.zip"
    assert reconciled["packages"][0]["status"] == "pending"


def test_save_batch_meta_to_gcs_retries_cas_conflict_preserving_cancelling(isolated_batch_fs, fake_gcs):
    batch_id = "e" * 32
    batch_dir = Path(main._batch_dir(batch_id))
    _write_meta(
        batch_dir,
        {
            "batchId": batch_id,
            "status": "cancelling",
            "metaRevision": 1,
            "cancelRequested": True,
            "packages": [{"name": "P1", "status": "processing"}],
        },
    )
    fake_gcs.objects[main._batch_meta_object_name(batch_id)] = {
        "data": json.dumps(
            {
                "batchId": batch_id,
                "status": "processing",
                "metaRevision": 2,
                "cancelRequested": False,
                "packages": [{"name": "P1", "status": "processing"}],
            },
            ensure_ascii=False,
            indent=2,
        ),
        "generation": 2,
    }
    fake_gcs.force_conflict_once = True

    local_meta = {
        "batchId": batch_id,
        "status": "cancelling",
        "metaRevision": 1,
        "cancelRequested": True,
        "packages": [{"name": "P1", "status": "processing"}],
    }

    result = main._save_batch_meta_to_gcs(batch_id, local_meta, final=True)

    assert result.success
    assert result.final_meta["status"] == "cancelling"
    assert result.final_meta["cancelRequested"] is True
    assert result.final_meta["metaRevision"] == 4
    assert result.observed_generation == 3
    assert [attempt[1] for attempt in fake_gcs.upload_attempts[:2]] == [2, 3]


def test_save_batch_meta_persists_cancelling_after_cas_conflict_retry(isolated_batch_fs, fake_gcs):
    batch_id = "f" * 32
    batch_dir = Path(main._batch_dir(batch_id))
    _write_meta(
        batch_dir,
        {
            "batchId": batch_id,
            "status": "cancelling",
            "metaRevision": 1,
            "cancelRequested": True,
            "packages": [{"name": "P1", "status": "processing"}],
        },
    )
    fake_gcs.objects[main._batch_meta_object_name(batch_id)] = {
        "data": json.dumps(
            {
                "batchId": batch_id,
                "status": "processing",
                "metaRevision": 2,
                "cancelRequested": False,
                "packages": [{"name": "P1", "status": "processing"}],
            },
            ensure_ascii=False,
            indent=2,
        ),
        "generation": 2,
    }
    fake_gcs.force_conflict_once = True

    local_meta = {
        "batchId": batch_id,
        "status": "cancelling",
        "metaRevision": 1,
        "cancelRequested": True,
        "packages": [{"name": "P1", "status": "processing"}],
    }

    persisted = main._save_batch_meta(batch_id, local_meta, final=True)
    disk_meta = json.loads((batch_dir / "meta.json").read_text(encoding="utf-8"))

    assert persisted == disk_meta
    assert disk_meta["status"] == "cancelling"
    assert disk_meta["cancelRequested"] is True
    assert disk_meta["metaRevision"] == 4
    assert "metaSyncError" not in disk_meta
    assert [attempt[1] for attempt in fake_gcs.upload_attempts[:2]] == [2, 3]


def test_process_batch_persists_worker_results_without_stale_package_reference(isolated_batch_fs, fake_gcs, monkeypatch):
    batch_id = "g" * 32
    batch_dir = Path(main._batch_dir(batch_id))
    results_dir = batch_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    _write_meta(
        batch_dir,
        {
            "batchId": batch_id,
            "service": "cuidador",
            "status": "pending",
            "metaRevision": 0,
            "cancelRequested": False,
            "packages": [
                {
                    "name": "P1",
                    "folder": "P1",
                    "status": "pending",
                    "startedAt": None,
                    "finishedAt": None,
                    "elapsedSeconds": None,
                    "jobId": None,
                    "resultFile": None,
                    "downloadName": None,
                    "error": None,
                    "gcsResult": None,
                    "currentStage": None,
                    "lastHeartbeatAt": None,
                    "audit": None,
                }
            ],
            "allZip": None,
            "gcsAllZip": None,
            "sourceGcsPath": None,
        },
    )

    stages = []

    def fake_worker(batch_id_arg, package_name, service):
        assert batch_id_arg == batch_id
        assert package_name == "P1"
        assert service == "cuidador"
        (results_dir / "P1.zip").write_text("pkg-zip", encoding="utf-8")
        return {"jobId": "job-1", "resultFile": "P1.zip", "downloadName": "P1.zip"}

    def fake_zip(batch_id_arg, meta):
        assert batch_id_arg == batch_id
        all_path = results_dir / "all.zip"
        all_path.write_text("all-zip", encoding="utf-8")
        meta["allZip"] = "all.zip"
        return str(all_path)

    def fake_log_timing(event, **fields):
        if event == "batch_timing":
            stages.append(fields)

    monkeypatch.setattr(main, "_run_batch_package_worker", fake_worker)
    monkeypatch.setattr(main, "_build_consolidated_batch_zip", fake_zip)
    monkeypatch.setattr(main, "_log_timing", fake_log_timing)
    monkeypatch.setattr(main.time, "sleep", lambda *_args, **_kwargs: None)

    main._process_batch(batch_id)

    local_meta = json.loads((batch_dir / "meta.json").read_text(encoding="utf-8"))
    remote_meta = json.loads(fake_gcs.objects[main._batch_meta_object_name(batch_id)]["data"])
    other_instance_meta = main._load_batch_meta_latest(batch_id)

    assert local_meta["status"] == "done"
    assert remote_meta["status"] == "done"
    assert other_instance_meta["status"] == "done"
    assert local_meta["packages"][0]["status"] == "done"
    assert local_meta["packages"][0]["jobId"] == "job-1"
    assert local_meta["packages"][0]["resultFile"] == "P1.zip"
    assert local_meta["packages"][0]["downloadName"] == "P1.zip"
    assert local_meta["packages"][0]["finishedAt"] is not None
    assert local_meta["packages"][0]["elapsedSeconds"] is not None
    assert local_meta["gcsAllZip"] == f"gs://{main.GCS_BUCKET}/results/{batch_id}/all.zip"
    assert local_meta["packages"][0]["gcsResult"] == f"gs://{main.GCS_BUCKET}/results/{batch_id}/P1.zip"
    assert remote_meta["gcsAllZip"] == f"gs://{main.GCS_BUCKET}/results/{batch_id}/all.zip"
    assert remote_meta["packages"][0]["jobId"] == "job-1"
    assert remote_meta["packages"][0]["resultFile"] == "P1.zip"
    assert remote_meta["packages"][0]["downloadName"] == "P1.zip"
    assert remote_meta["packages"][0]["finishedAt"] is not None
    assert remote_meta["packages"][0]["elapsedSeconds"] is not None
    assert remote_meta["packages"][0]["gcsResult"] == f"gs://{main.GCS_BUCKET}/results/{batch_id}/P1.zip"
    assert any(name == main._batch_meta_object_name(batch_id) for name, *_ in fake_gcs.upload_attempts)
    assert any(name == f"results/{batch_id}/P1.zip" for name, *_ in fake_gcs.upload_attempts)
    assert any(name == f"results/{batch_id}/all.zip" for name, *_ in fake_gcs.upload_attempts)

    batch_done = next(stage for stage in stages if stage.get("stage") == "batch_done")
    assert batch_done["status"] == "done"
    assert batch_done["done"] == 1
    assert batch_done["errors"] == 0

    shutil.rmtree(batch_dir, ignore_errors=True)
    reopened_meta = main._load_batch_meta_latest(batch_id)
    assert reopened_meta["status"] == "done"
    assert reopened_meta["packages"][0]["status"] == "done"
    assert reopened_meta["packages"][0]["jobId"] == "job-1"
    assert reopened_meta["packages"][0]["resultFile"] == "P1.zip"
    assert reopened_meta["packages"][0]["downloadName"] == "P1.zip"
    assert reopened_meta["packages"][0]["finishedAt"] is not None
    assert reopened_meta["packages"][0]["elapsedSeconds"] is not None


@pytest.mark.parametrize(
    ("worker_exc", "expected_status", "expected_error", "expected_stage", "expected_done", "expected_errors"),
    [
        (RuntimeError("boom"), "error", "boom", "error", 0, 1),
        (RuntimeError("batch_cancelled"), "cancelled", "cancelled", "cancelled", 0, 0),
    ],
    ids=["worker-error", "worker-cancelled"],
)
def test_process_batch_reacquires_package_reference_for_error_and_cancel_paths(
    isolated_batch_fs,
    fake_gcs,
    monkeypatch,
    worker_exc,
    expected_status,
    expected_error,
    expected_stage,
    expected_done,
    expected_errors,
):
    batch_id = "h" * 32
    batch_dir = Path(main._batch_dir(batch_id))
    results_dir = batch_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    _write_meta(
        batch_dir,
        {
            "batchId": batch_id,
            "service": "cuidador",
            "status": "pending",
            "metaRevision": 0,
            "cancelRequested": False,
            "packages": [
                {
                    "name": "P1",
                    "folder": "P1",
                    "status": "pending",
                    "startedAt": None,
                    "finishedAt": None,
                    "elapsedSeconds": None,
                    "jobId": None,
                    "resultFile": None,
                    "downloadName": None,
                    "error": None,
                    "gcsResult": None,
                    "currentStage": None,
                    "lastHeartbeatAt": None,
                    "audit": None,
                }
            ],
            "allZip": None,
            "gcsAllZip": None,
            "sourceGcsPath": None,
        },
    )

    stages = []

    def fake_worker(*_args, **_kwargs):
        raise worker_exc

    def fake_zip(batch_id_arg, meta):
        assert batch_id_arg == batch_id
        all_path = results_dir / "all.zip"
        all_path.write_text("all-zip", encoding="utf-8")
        meta["allZip"] = "all.zip"
        return str(all_path)

    def fake_log_timing(event, **fields):
        if event == "batch_timing":
            stages.append(fields)

    monkeypatch.setattr(main, "_run_batch_package_worker", fake_worker)
    monkeypatch.setattr(main, "_build_consolidated_batch_zip", fake_zip)
    monkeypatch.setattr(main, "_log_timing", fake_log_timing)
    monkeypatch.setattr(main.time, "sleep", lambda *_args, **_kwargs: None)

    main._process_batch(batch_id)

    local_meta = json.loads((batch_dir / "meta.json").read_text(encoding="utf-8"))
    remote_meta = json.loads(fake_gcs.objects[main._batch_meta_object_name(batch_id)]["data"])

    assert local_meta["status"] == expected_status
    assert remote_meta["status"] == expected_status
    assert local_meta["packages"][0]["status"] == expected_status
    assert remote_meta["packages"][0]["status"] == expected_status
    assert local_meta["packages"][0]["error"] == expected_error
    assert remote_meta["packages"][0]["error"] == expected_error
    assert local_meta["packages"][0]["currentStage"] == expected_stage
    assert remote_meta["packages"][0]["currentStage"] == expected_stage
    assert local_meta["gcsAllZip"] == f"gs://{main.GCS_BUCKET}/results/{batch_id}/all.zip"
    assert remote_meta["gcsAllZip"] == f"gs://{main.GCS_BUCKET}/results/{batch_id}/all.zip"
    assert any(name == f"results/{batch_id}/all.zip" for name, *_ in fake_gcs.upload_attempts)

    batch_done = next(stage for stage in stages if stage.get("stage") == "batch_done")
    assert batch_done["status"] == expected_status
    assert batch_done["done"] == expected_done
    assert batch_done["errors"] == expected_errors
