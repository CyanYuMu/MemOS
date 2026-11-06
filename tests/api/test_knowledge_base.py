"""T3.1 knowledge base MVP: tenant isolation, storage limits, cleanup."""


import base64

from pathlib import Path

import pytest

from memos.api.cloud_store import JsonMetadataStore
from memos.api.knowledge_base import KnowledgeBaseService, NoopIngestor, public_file_view


def _service(tmp_path):
    return KnowledgeBaseService(
        store=JsonMetadataStore(tmp_path / "store"),
        storage_dir=tmp_path / "kb",
        ingestor=NoopIngestor(),
    )


def _kb(service, tenant="tenant-1", name="docs"):
    return service.create(user_name=tenant, name=name, owner_user_id=tenant)


def test_create_list_get_and_delete(tmp_path):
    service = _service(tmp_path)
    document = _kb(service)

    assert service.get(user_name="tenant-1", knowledgebase_id=document["entity_id"])
    listed, total = service.list(user_name="tenant-1")
    assert total == 1
    assert listed[0]["name"] == "docs"

    assert service.delete(user_name="tenant-1", knowledgebase_id=document["entity_id"]) is True
    assert service.get(user_name="tenant-1", knowledgebase_id=document["entity_id"]) is None


def test_knowledge_bases_are_tenant_scoped(tmp_path):
    service = _service(tmp_path)
    document = _kb(service, tenant="tenant-1")

    assert service.get(user_name="tenant-2", knowledgebase_id=document["entity_id"]) is None
    assert service.list(user_name="tenant-2") == ([], 0)
    assert service.delete(user_name="tenant-2", knowledgebase_id=document["entity_id"]) is False
    # The original owner still sees it.
    assert service.get(user_name="tenant-1", knowledgebase_id=document["entity_id"])


def test_blank_name_is_rejected(tmp_path):
    service = _service(tmp_path)

    with pytest.raises(ValueError, match="required"):
        service.create(user_name="tenant-1", name="   ")


def test_update_renames_and_replaces_description(tmp_path):
    service = _service(tmp_path)
    kb = _kb(service)
    original_updated_at = kb["updated_at"]

    renamed = service.update(
        user_name="tenant-1",
        knowledgebase_id=kb["entity_id"],
        name="handbook",
        description="ops",
    )

    assert renamed["name"] == "handbook"
    assert renamed["description"] == "ops"
    assert renamed["updated_at"] >= original_updated_at
    assert service.get(user_name="tenant-2", knowledgebase_id=kb["entity_id"]) is None


def test_update_rejects_blank_name(tmp_path):
    service = _service(tmp_path)
    kb = _kb(service)

    with pytest.raises(ValueError, match="required"):
        service.update(user_name="tenant-1", knowledgebase_id=kb["entity_id"], name="  ")


def test_upload_stores_bytes_and_updates_stats(tmp_path):
    service = _service(tmp_path)
    kb = _kb(service)

    files = service.add_files(
        user_name="tenant-1",
        knowledgebase_id=kb["entity_id"],
        files=[{"name": "notes.md", "content": "hello kb"}],
    )

    assert len(files) == 1
    stored = Path(files[0]["storage_path"])
    assert stored.read_bytes() == b"hello kb"
    assert files[0]["status"] == "stored"

    refreshed = service.get(user_name="tenant-1", knowledgebase_id=kb["entity_id"])
    assert refreshed["file_count"] == 1
    assert refreshed["total_size"] == len(b"hello kb")


def test_base64_upload_is_decoded(tmp_path):
    service = _service(tmp_path)
    kb = _kb(service)

    files = service.add_files(
        user_name="tenant-1",
        knowledgebase_id=kb["entity_id"],
        files=[{"name": "a.txt", "base64": "aGVsbG8="}],
    )

    assert Path(files[0]["storage_path"]).read_bytes() == b"hello"


def test_unsupported_extension_is_rejected_before_writing(tmp_path):
    service = _service(tmp_path)
    kb = _kb(service)

    with pytest.raises(ValueError, match="unsupported file type"):
        service.add_files(
            user_name="tenant-1",
            knowledgebase_id=kb["entity_id"],
            files=[{"name": "malware.exe", "content": "x"}],
        )
    assert service.list_files(user_name="tenant-1", knowledgebase_id=kb["entity_id"]) == ([], 0)


def test_upload_count_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_MAX_FILES_PER_UPLOAD", "2")
    service = _service(tmp_path)
    kb = _kb(service)

    with pytest.raises(ValueError, match="at most 2"):
        service.add_files(
            user_name="tenant-1",
            knowledgebase_id=kb["entity_id"],
            files=[{"name": f"{i}.txt", "content": "x"} for i in range(3)],
        )


def test_per_file_size_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_MAX_FILE_SIZE_MB", "1")
    service = _service(tmp_path)
    kb = _kb(service)

    with pytest.raises(ValueError, match="1 MB"):
        service.add_files(
            user_name="tenant-1",
            knowledgebase_id=kb["entity_id"],
            files=[{"name": "big.txt", "content": b"x" * (1024 * 1024 + 1)}],
        )


def test_page_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_MAX_FILE_PAGES", "500")
    service = _service(tmp_path)
    kb = _kb(service)

    with pytest.raises(ValueError, match="500 page"):
        service.add_files(
            user_name="tenant-1",
            knowledgebase_id=kb["entity_id"],
            files=[{"name": "big.pdf", "content": b"%PDF", "pages": 501}],
        )


def test_upload_to_foreign_kb_is_rejected(tmp_path):
    service = _service(tmp_path)
    kb = _kb(service, tenant="tenant-1")

    with pytest.raises(ValueError, match="not found"):
        service.add_files(
            user_name="tenant-2",
            knowledgebase_id=kb["entity_id"],
            files=[{"name": "a.txt", "content": "x"}],
        )


def test_path_traversal_in_filename_is_neutralised(tmp_path):
    service = _service(tmp_path)
    kb = _kb(service)

    files = service.add_files(
        user_name="tenant-1",
        knowledgebase_id=kb["entity_id"],
        files=[{"name": "../../etc/passwd.txt", "content": "x"}],
    )

    stored = Path(files[0]["storage_path"])
    assert files[0]["name"] == "passwd.txt"
    assert (tmp_path / "kb") in stored.parents


def test_list_files_filters_and_paginates(tmp_path):
    service = _service(tmp_path)
    kb = _kb(service)
    service.add_files(
        user_name="tenant-1",
        knowledgebase_id=kb["entity_id"],
        files=[{"name": f"{i}.txt", "content": "x"} for i in range(5)],
    )

    page, total = service.list_files(
        user_name="tenant-1", knowledgebase_id=kb["entity_id"], page=2, page_size=2
    )
    assert total == 5
    assert len(page) == 2

    filtered, total = service.list_files(user_name="tenant-1", file_type="manual")
    assert (filtered, total) == ([], 0)


def test_delete_file_removes_bytes_and_stats(tmp_path):
    service = _service(tmp_path)
    kb = _kb(service)
    file_doc = service.add_files(
        user_name="tenant-1",
        knowledgebase_id=kb["entity_id"],
        files=[{"name": "a.txt", "content": "x"}],
    )[0]

    assert service.delete_files(user_name="tenant-1", file_ids=[file_doc["entity_id"]]) == 1

    assert not Path(file_doc["storage_path"]).exists()
    assert service.get_file(user_name="tenant-1", file_id=file_doc["entity_id"]) is None
    assert service.get(user_name="tenant-1", knowledgebase_id=kb["entity_id"])["file_count"] == 0


def test_delete_kb_removes_its_files_only(tmp_path):
    service = _service(tmp_path)
    kb1 = _kb(service, name="one")
    kb2 = _kb(service, name="two")
    file1 = service.add_files(
        user_name="tenant-1",
        knowledgebase_id=kb1["entity_id"],
        files=[{"name": "a.txt", "content": "x"}],
    )[0]
    file2 = service.add_files(
        user_name="tenant-1",
        knowledgebase_id=kb2["entity_id"],
        files=[{"name": "b.txt", "content": "y"}],
    )[0]

    service.delete(user_name="tenant-1", knowledgebase_id=kb1["entity_id"])

    assert not Path(file1["storage_path"]).exists()
    assert Path(file2["storage_path"]).exists()
    assert service.get_file(user_name="tenant-1", file_id=file2["entity_id"])


def test_download_reads_bytes_for_the_owner_only(tmp_path):
    service = _service(tmp_path)
    kb = _kb(service)
    file_doc = service.add_files(
        user_name="tenant-1",
        knowledgebase_id=kb["entity_id"],
        files=[{"name": "a.txt", "content": "payload"}],
    )[0]

    content, document = service.read_file(user_name="tenant-1", file_id=file_doc["entity_id"])
    assert content == b"payload"
    assert document["name"] == "a.txt"
    assert service.read_file(user_name="tenant-2", file_id=file_doc["entity_id"]) is None


def test_remote_url_is_not_fetched_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("KB_FETCH_REMOTE_FILES", raising=False)
    service = _service(tmp_path)
    kb = _kb(service)

    file_doc = service.add_files(
        user_name="tenant-1",
        knowledgebase_id=kb["entity_id"],
        files=[{"name": "remote.txt", "content": "https://example.com/a.txt"}],
    )[0]

    assert Path(file_doc["storage_path"]).read_bytes() == b""
    assert file_doc["source_url"] == "https://example.com/a.txt"


def test_noop_ingestor_reports_stored_without_chunks():
    result = NoopIngestor().ingest(
        user_name="tenant-1", knowledgebase={"entity_id": "kb_1"}, file={}, content=b"x"
    )

    assert result["status"] == "stored"
    assert result["chunks"] == 0


def test_public_file_view_shape(tmp_path):
    service = _service(tmp_path)
    kb = _kb(service)
    file_doc = service.add_files(
        user_name="tenant-1",
        knowledgebase_id=kb["entity_id"],
        files=[{"name": "a.txt", "content": "1234"}],
    )[0]

    view = public_file_view(file_doc)

    assert view["id"] == file_doc["entity_id"]
    assert view["name"] == "a.txt"
    assert view["size"] == 4
    assert view["sizeMB"] == pytest.approx(0.000004, abs=1e-9)


def test_batch_is_validated_before_any_file_is_written(tmp_path):
    service = _service(tmp_path)
    kb = _kb(service)

    with pytest.raises(ValueError, match="unsupported file type"):
        service.add_files(
            user_name="tenant-1",
            knowledgebase_id=kb["entity_id"],
            files=[
                {"name": "good.txt", "content": "hello"},
                {"name": "evil.exe", "content": "nope"},
            ],
        )

    listed, total = service.list_files(user_name="tenant-1", knowledgebase_id=kb["entity_id"])
    assert (listed, total) == ([], 0)
    assert list((tmp_path / "kb").rglob("*.*")) == []
    assert service.get(user_name="tenant-1", knowledgebase_id=kb["entity_id"])["file_count"] == 0


def test_url_only_file_derives_its_extension(tmp_path):
    service = _service(tmp_path)
    kb = _kb(service)

    [document] = service.add_files(
        user_name="tenant-1",
        knowledgebase_id=kb["entity_id"],
        files=[{"name": "spec", "url": "https://example.com/docs/spec.pdf"}],
    )

    assert document["name"] == "spec.pdf"
    assert document["ext"] == "pdf"


def test_private_urls_are_refused(tmp_path, monkeypatch):
    from memos.api.knowledge_base import _is_public_url

    monkeypatch.delenv("KB_ALLOW_PRIVATE_URLS", raising=False)
    assert _is_public_url("http://127.0.0.1/secret") is False
    assert _is_public_url("http://10.0.0.1/secret") is False


def test_read_file_refuses_paths_outside_the_storage_root(tmp_path):
    service = _service(tmp_path)
    kb = _kb(service)
    [document] = service.add_files(
        user_name="tenant-1",
        knowledgebase_id=kb["entity_id"],
        files=[{"name": "a.txt", "content": "hello"}],
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    document = service.store.get("knowledgebase_file", document["entity_id"])
    document["storage_path"] = str(outside)
    service.store.put("knowledgebase_file", document)

    assert service.read_file(user_name="tenant-1", file_id=document["entity_id"]) is None


def test_remote_fetch_requires_an_explicit_host_allowlist(monkeypatch):
    from memos.api.knowledge_base import _remote_host_allowed

    monkeypatch.setenv("KB_REMOTE_ALLOWED_HOSTS", "files.example.com")
    assert _remote_host_allowed("https://files.example.com/a.pdf") is True
    assert _remote_host_allowed("https://evil.example.com/a.pdf") is False

    monkeypatch.delenv("KB_REMOTE_ALLOWED_HOSTS")
    assert _remote_host_allowed("https://files.example.com/a.pdf") is False


def test_read_file_refuses_files_over_the_size_limit(tmp_path, monkeypatch):
    service = _service(tmp_path)
    kb = _kb(service)
    [file_doc] = service.add_files(
        user_name="tenant-1",
        knowledgebase_id=kb["entity_id"],
        files=[{"name": "a.txt", "content": "x"}],
    )
    stored = Path(file_doc["storage_path"])
    stored.write_bytes(b"x" * (1024 * 1024 + 1))
    monkeypatch.setenv("KB_MAX_FILE_SIZE_MB", "1")

    with pytest.raises(ValueError, match="1 MB download limit"):
        service.read_file(user_name="tenant-1", file_id=file_doc["entity_id"])


def test_skill_file_type_is_rejected(tmp_path):
    service = _service(tmp_path)
    kb = _kb(service)
    with pytest.raises(ValueError, match="unsupported file type"):
        service.add_files(user_name="tenant-1", knowledgebase_id=kb["entity_id"], files=[{"name": "x.md", "content": "data", "type": "skill"}])


def test_file_type_is_validated_per_document(tmp_path):
    service = _service(tmp_path)
    kb = _kb(service)
    args = {"user_name": "tenant-1", "knowledgebase_id": kb["entity_id"]}
    with pytest.raises(ValueError, match="unsupported file type"):
        service.add_files(**args, files=[{"name": "n.txt", "bytes": b"x", "type": "video"}])


def test_base64_document_data_url_is_decoded(tmp_path):
    service = _service(tmp_path)
    kb = _kb(service)
    encoded = base64.b64encode(b"document content").decode()
    [document] = service.add_files(
        user_name="tenant-1",
        knowledgebase_id=kb["entity_id"],
        files=[{"name": "notes.txt", "content": f"data:text/plain;base64,{encoded}"}],
    )
    assert Path(document["storage_path"]).read_bytes() == b"document content"
