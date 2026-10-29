"""Contract tests for the object-storage backends of the cloud store.

The store keeps files in Google Cloud Storage by default and in an Azure Blob Storage
container when ``INV_STUDIO_STORAGE_BACKEND=azure``. Both backends are exercised against
fake clients: no Azure or Google package is needed and nothing talks to a network.
"""
import os
import stat
import sys

import pytest
from cryptography.fernet import Fernet

from app.cloud_store import AzureContainer, PostgresStore, _azure_account_url, _is_missing


class FakeCursor:
    description = None

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __iter__(self):
        return iter(())


class FakeConnection:
    """Just enough of a psycopg connection for the store to create its schema."""

    def __init__(self):
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append(sql)
        return FakeCursor()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class FakeAzureError(Exception):
    def __init__(self, status_code):
        super().__init__(f"azure error {status_code}")
        self.status_code = status_code


class FakeDownload:
    def __init__(self, data):
        self.data = data

    def readinto(self, handle):
        handle.write(self.data)
        return len(self.data)


class FakeBlobClient:
    def __init__(self, container, name):
        self.container = container
        self.blob_name = name

    def download_blob(self):
        if self.blob_name not in self.container.blobs:
            raise FakeAzureError(404)
        return FakeDownload(self.container.blobs[self.blob_name])

    def upload_blob(self, stream, overwrite=False):
        if not overwrite and self.blob_name in self.container.blobs:
            raise FakeAzureError(409)
        self.container.blobs[self.blob_name] = stream.read()
        self.container.uploads.append(self.blob_name)

    def delete_blob(self):
        if self.blob_name not in self.container.blobs:
            raise FakeAzureError(self.container.delete_error)
        del self.container.blobs[self.blob_name]


class FakeBlobProperties:
    def __init__(self, name):
        self.name = name


class FakeContainerClient:
    def __init__(self, name):
        self.container_name = name
        self.blobs = {}
        self.uploads = []
        self.delete_error = 404

    def get_blob_client(self, name):
        return FakeBlobClient(self, name)

    def list_blobs(self, name_starts_with=None):
        for name in sorted(self.blobs):
            if name_starts_with is None or name.startswith(name_starts_with):
                yield FakeBlobProperties(name)


class FakeBlobServiceClient:
    def __init__(self):
        self.containers = {}

    def get_container_client(self, name):
        return self.containers.setdefault(name, FakeContainerClient(name))


class FakeGcsBlob:
    def __init__(self, bucket, name):
        self.bucket = bucket
        self.name = name

    def upload_from_filename(self, filename):
        with open(filename, "rb") as handle:
            self.bucket.blobs[self.name] = handle.read()


class FakeGcsBucket:
    def __init__(self, name):
        self.name = name
        self.blobs = {}

    def blob(self, name):
        return FakeGcsBlob(self, name)


class FakeGcsClient:
    def __init__(self):
        self.buckets = {}

    def bucket(self, name):
        return self.buckets.setdefault(name, FakeGcsBucket(name))


@pytest.fixture
def cloud_env(monkeypatch):
    monkeypatch.setenv("INV_STUDIO_DATABASE_URL", "postgresql://fake/invstudio")
    monkeypatch.setenv("INV_STUDIO_BUCKET", "evidence")
    monkeypatch.setenv("INV_STUDIO_VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("INV_STUDIO_STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("INV_STUDIO_AZURE_STORAGE_URL", raising=False)
    monkeypatch.delenv("INV_STUDIO_AZURE_STORAGE_ACCOUNT", raising=False)
    monkeypatch.delenv("INV_STUDIO_AZURE_STORAGE_SUFFIX", raising=False)
    monkeypatch.delenv("INV_STUDIO_AZURE_STORAGE_CONNECTION_STRING", raising=False)


def make_store(root, client):
    return PostgresStore(root, _connect=lambda url: FakeConnection(), _storage_client=client)


@pytest.fixture
def azure_store(tmp_path, cloud_env, monkeypatch):
    monkeypatch.setenv("INV_STUDIO_STORAGE_BACKEND", "azure")
    client = FakeBlobServiceClient()
    store = make_store(tmp_path / "data", client)
    return store, client.containers["evidence"]


def test_google_storage_is_the_default(tmp_path, cloud_env):
    store = make_store(tmp_path / "data", FakeGcsClient())
    assert store.storage_backend == "gcs"
    assert isinstance(store.bucket, FakeGcsBucket)
    assert store.bucket.name == "evidence"


@pytest.mark.parametrize("value", ["GCS", " gcs ", ""])
def test_google_storage_names_are_accepted(tmp_path, cloud_env, monkeypatch, value):
    monkeypatch.setenv("INV_STUDIO_STORAGE_BACKEND", value)
    assert make_store(tmp_path / "data", FakeGcsClient()).storage_backend == "gcs"


def test_unknown_backend_is_refused(tmp_path, cloud_env, monkeypatch):
    monkeypatch.setenv("INV_STUDIO_STORAGE_BACKEND", "s3")
    with pytest.raises(RuntimeError, match="INV_STUDIO_STORAGE_BACKEND"):
        make_store(tmp_path / "data", FakeGcsClient())


def test_azure_backend_uses_the_bucket_name_as_the_container(azure_store):
    store, container = azure_store
    assert store.storage_backend == "azure"
    assert isinstance(store.bucket, AzureContainer)
    assert container.container_name == "evidence"


def test_upload_is_stored_under_its_posix_name(azure_store):
    store, container = azure_store
    upload = store.root / "uploads" / "invoice.pdf"
    upload.write_bytes(b"%PDF-1.4 one")
    assert store.persist_blob(upload) == "uploads/invoice.pdf"
    assert container.blobs == {"uploads/invoice.pdf": b"%PDF-1.4 one"}


def test_persist_overwrites_an_existing_blob(azure_store):
    store, container = azure_store
    upload = store.root / "uploads" / "invoice.pdf"
    upload.write_bytes(b"first")
    store.persist_blob(upload)
    upload.write_bytes(b"second")
    store.persist_blob(upload)
    assert container.blobs["uploads/invoice.pdf"] == b"second"


def test_persist_refuses_a_file_that_is_not_there(azure_store):
    store, _ = azure_store
    with pytest.raises(ValueError, match="existing files"):
        store.persist_blob(store.root / "uploads" / "gone.pdf")


def test_ensure_blob_downloads_the_file_once(azure_store):
    store, container = azure_store
    container.blobs["uploads/invoice.pdf"] = b"%PDF-1.4 downloaded"
    target = store.ensure_blob("uploads/invoice.pdf")
    assert target == store.root / "uploads" / "invoice.pdf"
    assert target.read_bytes() == b"%PDF-1.4 downloaded"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    container.blobs["uploads/invoice.pdf"] = b"changed in the container"
    assert store.ensure_blob("uploads/invoice.pdf").read_bytes() == b"%PDF-1.4 downloaded"


def test_ensure_blob_leaves_no_partial_file_when_the_blob_is_missing(azure_store):
    store, _ = azure_store
    with pytest.raises(FakeAzureError):
        store.ensure_blob("uploads/missing.pdf")
    assert sorted(p.name for p in (store.root / "uploads").iterdir()) == []


def test_delete_upload_removes_both_copies(azure_store):
    store, container = azure_store
    upload = store.root / "uploads" / "invoice.pdf"
    upload.write_bytes(b"bytes")
    store.persist_blob(upload)
    store.delete_upload(upload)
    assert container.blobs == {}
    assert not upload.exists()


def test_delete_upload_is_happy_when_the_blob_is_already_gone(azure_store):
    store, _ = azure_store
    upload = store.root / "uploads" / "invoice.pdf"
    upload.write_bytes(b"bytes")
    store.delete_upload(upload)
    assert not upload.exists()


def test_delete_upload_reports_a_real_storage_failure(azure_store):
    store, container = azure_store
    container.delete_error = 403
    upload = store.root / "uploads" / "invoice.pdf"
    upload.write_bytes(b"bytes")
    with pytest.raises(FakeAzureError):
        store.delete_upload(upload)
    assert upload.exists()


def test_delete_upload_is_only_for_uploads(azure_store):
    store, _ = azure_store
    learned = store.root / "learned" / "note.json"
    learned.parent.mkdir(parents=True, exist_ok=True)
    learned.write_text("{}")
    with pytest.raises(ValueError, match="invoice uploads"):
        store.delete_upload(learned)


def test_remove_blob_is_only_for_the_learned_store(azure_store):
    store, container = azure_store
    learned = store.root / "learned" / "note.json"
    learned.parent.mkdir(parents=True, exist_ok=True)
    learned.write_text("{}")
    store.persist_blob(learned)
    store.remove_blob(learned)
    assert container.blobs == {}
    upload = store.root / "uploads" / "invoice.pdf"
    upload.write_bytes(b"bytes")
    with pytest.raises(ValueError, match="learned-store"):
        store.remove_blob(upload)


@pytest.mark.parametrize("path", ["exports/book.xlsx", "work/scratch.txt", "../escape.pdf"])
def test_only_persisted_directories_reach_the_container(azure_store, path):
    store, _ = azure_store
    with pytest.raises(ValueError):
        store.persist_blob(path)


def test_sync_templates_restores_templates_and_the_learned_store(azure_store):
    store, container = azure_store
    container.blobs.update({
        "templates/acme.yml": b"issuer: acme",
        "learned/acme/fields.json": b"{\"total\": 1}",
        "uploads/invoice.pdf": b"not synced",
    })
    kept = store.root / "templates" / "acme.yml"
    kept.write_text("local copy wins")
    store.sync_templates()
    assert kept.read_text() == "local copy wins"
    learned = store.root / "learned" / "acme" / "fields.json"
    assert learned.read_bytes() == b"{\"total\": 1}"
    assert not (store.root / "uploads" / "invoice.pdf").exists()


@pytest.mark.parametrize("name", ["templates/../../escape.yml", "templates/", "learned/a/../../escape"])
def test_sync_templates_refuses_an_unsafe_blob_name(azure_store, name):
    store, container = azure_store
    container.blobs[name] = b"x"
    with pytest.raises(ValueError, match="unsafe name"):
        store.sync_templates()


def test_account_url_is_built_from_the_account_name(cloud_env, monkeypatch):
    monkeypatch.setenv("INV_STUDIO_AZURE_STORAGE_ACCOUNT", "invstudioevidence")
    assert _azure_account_url() == "https://invstudioevidence.blob.core.windows.net"
    monkeypatch.setenv("INV_STUDIO_AZURE_STORAGE_SUFFIX", "blob.core.usgovcloudapi.net")
    assert _azure_account_url() == "https://invstudioevidence.blob.core.usgovcloudapi.net"
    monkeypatch.setenv("INV_STUDIO_AZURE_STORAGE_URL", "https://other.blob.core.windows.net")
    assert _azure_account_url() == "https://other.blob.core.windows.net"


def test_account_url_needs_the_account_name(cloud_env):
    with pytest.raises(RuntimeError, match="INV_STUDIO_AZURE_STORAGE_ACCOUNT"):
        _azure_account_url()


def test_missing_azure_package_is_explained(tmp_path, cloud_env, monkeypatch):
    monkeypatch.setenv("INV_STUDIO_STORAGE_BACKEND", "azure")
    monkeypatch.setenv("INV_STUDIO_AZURE_STORAGE_ACCOUNT", "invstudioevidence")
    monkeypatch.setitem(sys.modules, "azure.storage.blob", None)
    with pytest.raises(RuntimeError, match="azure-storage-blob"):
        PostgresStore(tmp_path / "data", _connect=lambda url: FakeConnection())


def test_a_missing_object_is_recognised_on_both_clouds():
    assert _is_missing(FakeAzureError(404))
    google_style = Exception("not found")
    google_style.code = 404
    assert _is_missing(google_style)
    assert not _is_missing(FakeAzureError(403))
    assert not _is_missing(Exception("boom"))


def test_the_data_directory_is_private(azure_store):
    store, _ = azure_store
    assert stat.S_IMODE(os.stat(store.root).st_mode) == 0o700
