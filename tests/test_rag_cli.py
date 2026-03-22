import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from zen_claw.cli.commands import app


def _patch_data_dir(monkeypatch, data_dir: Path) -> None:
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)


def test_knowledge_stats_cli_prints_totals(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    _patch_data_dir(monkeypatch, tmp_path)

    class _FakePipeline:
        def __init__(self, data_dir, default_notebook="default", store_kind="chroma", tenant_id="default"):
            self.data_dir = data_dir
            self.tenant_id = tenant_id

        def stats(self, *, notebook_id: str = ""):
            return {
                "rag_available": True,
                "error": "",
                "tenant_id": self.tenant_id,
                "notebooks": [
                    {
                        "name": notebook_id or "default",
                        "doc_count": 2,
                        "chunk_count": 5,
                        "store_backend": "memory",
                    }
                ],
                "total_notebooks": 1,
                "total_documents": 2,
                "total_chunks": 5,
            }

    monkeypatch.setattr("zen_claw.knowledge.pipeline.RAGPipeline", _FakePipeline)
    out = runner.invoke(app, ["knowledge", "stats", "--notebook", "finance", "--tenant", "default"])
    assert out.exit_code == 0
    assert "Knowledge Stats" in out.output
    assert "total notebooks=1 documents=2 chunks=5 rag_available=True" in out.output
    assert "memory" in out.output


def test_rag_alias_search_reuses_knowledge_command(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    _patch_data_dir(monkeypatch, tmp_path)

    class _FakeSearchTool:
        def __init__(
            self,
            data_dir: Path,
            default_notebook: str = "default",
            tenant_id: str = "default",
            store_kind: str = "",
        ):
            self.data_dir = data_dir
            self.default_notebook = default_notebook
            self.tenant_id = tenant_id
            self.store_kind = store_kind

        async def execute(self, query: str, notebook_id: str = "", top_k: int = 5, **kwargs):
            assert kwargs.get("tenant_id") == self.tenant_id
            assert kwargs.get("store_backend") == "memory"
            return SimpleNamespace(
                ok=True,
                error=None,
                content=json.dumps(
                    {
                        "query": query,
                        "results": [
                            {"content": "grounded text", "source": "kb.md", "score": 0.9}
                        ],
                    }
                ),
            )

    monkeypatch.setattr("zen_claw.agent.tools.knowledge.KnowledgeSearchTool", _FakeSearchTool)
    out = runner.invoke(
        app,
        [
            "rag",
            "search",
            "policy",
            "--notebook",
            "finance",
            "--tenant",
            "default",
            "--store-backend",
            "memory",
        ],
    )
    assert out.exit_code == 0
    assert "kb.md" in out.output


def test_knowledge_add_and_search_accept_metadata_filter_json(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    _patch_data_dir(monkeypatch, tmp_path)

    class _FakeAddTool:
        def __init__(self, data_dir: Path, tenant_id: str = "default", store_kind: str = ""):
            self.data_dir = data_dir
            self.tenant_id = tenant_id
            self.store_kind = store_kind

        async def execute(self, source: str, notebook_id: str = "default", metadata=None, **kwargs):
            assert kwargs.get("tenant_id") == self.tenant_id
            return SimpleNamespace(
                ok=True,
                error=None,
                content=json.dumps(
                    {
                        "source": source,
                        "notebook": notebook_id,
                        "documents": 1,
                        "chunks_added": 2,
                        "metadata": metadata or {},
                    }
                ),
            )

    class _FakeSearchTool:
        def __init__(
            self,
            data_dir: Path,
            default_notebook: str = "default",
            tenant_id: str = "default",
            store_kind: str = "",
        ):
            self.data_dir = data_dir
            self.default_notebook = default_notebook
            self.tenant_id = tenant_id
            self.store_kind = store_kind

        async def execute(self, query: str, notebook_id: str = "", top_k: int = 5, filters=None, **kwargs):
            assert kwargs.get("tenant_id") == self.tenant_id
            return SimpleNamespace(
                ok=True,
                error=None,
                content=json.dumps(
                    {
                        "query": query,
                        "filters": filters or {},
                        "results": [{"content": "grounded text", "source": "kb.md", "score": 0.9}],
                    }
                ),
            )

    monkeypatch.setattr("zen_claw.agent.tools.knowledge.KnowledgeAddTool", _FakeAddTool)
    monkeypatch.setattr("zen_claw.agent.tools.knowledge.KnowledgeSearchTool", _FakeSearchTool)

    add_out = runner.invoke(
        app,
        [
            "knowledge",
            "add",
            str(tmp_path / "docs"),
            "--notebook",
            "finance",
            "--tenant",
            "default",
            "--metadata-json",
            '{"industry":"finance","tenant_id":"t1"}',
        ],
    )
    assert add_out.exit_code == 0

    search_out = runner.invoke(
        app,
        [
            "knowledge",
            "search",
            "policy",
            "--notebook",
            "finance",
            "--tenant",
            "default",
            "--filters-json",
            '{"industry":"finance"}',
        ],
    )
    assert search_out.exit_code == 0
    assert 'filters={"industry": "finance"}' in search_out.output


def test_rag_delete_alias_calls_pipeline(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    _patch_data_dir(monkeypatch, tmp_path)

    class _FakePipeline:
        def __init__(self, data_dir, default_notebook="default", store_kind="chroma", tenant_id="default"):
            self.data_dir = data_dir
            self.tenant_id = tenant_id

        def delete_document(self, document_id: str, *, notebook_id: str = ""):
            assert self.tenant_id == "default"
            assert notebook_id == "finance"
            return {
                "ok": True,
                "document_id": document_id,
                "source": document_id,
                "tenant_id": self.tenant_id,
                "notebook": notebook_id,
                "notebook_id": notebook_id,
                "documents_removed": 1,
                "chunks_deleted": 3,
            }

    monkeypatch.setattr("zen_claw.knowledge.pipeline.RAGPipeline", _FakePipeline)

    out = runner.invoke(
        app,
        ["rag", "delete", "finance_policy.md", "--notebook", "finance", "--tenant", "default"],
    )
    assert out.exit_code == 0
    assert "Removed" in out.output
    assert "3 chunk(s)" in out.output


def test_rag_documents_alias_lists_stable_document_ids(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    _patch_data_dir(monkeypatch, tmp_path)

    class _FakePipeline:
        def __init__(self, data_dir, default_notebook="default", store_kind="chroma", tenant_id="default"):
            self.data_dir = data_dir
            self.tenant_id = tenant_id

        def list_documents(self, *, notebook_id: str = ""):
            assert self.tenant_id == "default"
            assert notebook_id == "finance"
            return {
                "tenant_id": self.tenant_id,
                "notebook": notebook_id,
                "notebook_id": notebook_id,
                "documents": [
                    {
                        "document_id": "doc-finance-1",
                        "source": "finance_policy.md",
                        "doc_units": 1,
                        "created_at": "2026-03-13T00:00:00+00:00",
                    }
                ],
                "total_documents": 1,
            }

    monkeypatch.setattr("zen_claw.knowledge.pipeline.RAGPipeline", _FakePipeline)

    out = runner.invoke(
        app,
        ["rag", "documents", "--notebook", "finance", "--tenant", "default"],
    )
    assert out.exit_code == 0
    assert "doc-finance-1" in out.output
    assert "finance_policy.md" in out.output


def test_rag_clear_alias_calls_pipeline(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    _patch_data_dir(monkeypatch, tmp_path)

    class _FakePipeline:
        def __init__(self, data_dir, default_notebook="default", store_kind="chroma", tenant_id="default"):
            self.data_dir = data_dir
            self.tenant_id = tenant_id

        def clear_notebook(self, *, notebook_id: str = ""):
            assert self.tenant_id == "default"
            assert notebook_id == "finance"
            return {
                "ok": True,
                "tenant_id": self.tenant_id,
                "notebook": notebook_id,
                "notebook_id": notebook_id,
                "documents_removed": 2,
                "chunks_deleted": 5,
                "document_ids": ["doc-a", "doc-b"],
            }

    monkeypatch.setattr("zen_claw.knowledge.pipeline.RAGPipeline", _FakePipeline)

    out = runner.invoke(
        app,
        ["rag", "clear", "--notebook", "finance", "--tenant", "default"],
    )
    assert out.exit_code == 0
    assert "Cleared" in out.output
    assert "5 chunk(s)" in out.output


def test_rag_retain_alias_calls_pipeline(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    _patch_data_dir(monkeypatch, tmp_path)

    class _FakeKnowledge:
        retention_max_documents = 3
        retention_max_age_days = 0

    class _FakeConfig:
        knowledge = _FakeKnowledge()

    class _FakePipeline:
        def __init__(self, data_dir, default_notebook="default", store_kind="chroma", tenant_id="default"):
            self.data_dir = data_dir
            self.tenant_id = tenant_id

        def run_retention(self, *, notebook_id: str = "", max_documents: int = 0, max_age_days: int = 0):
            assert self.tenant_id == "default"
            assert notebook_id == "finance"
            assert max_documents == 3
            assert max_age_days == 0
            return {
                "ok": True,
                "tenant_id": self.tenant_id,
                "notebook": notebook_id,
                "notebook_id": notebook_id,
                "max_documents": max_documents,
                "max_age_days": max_age_days,
                "documents_removed": 2,
                "chunks_deleted": 4,
                "document_ids": ["doc-a", "doc-b"],
            }

    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda: _FakeConfig())
    monkeypatch.setattr("zen_claw.knowledge.pipeline.RAGPipeline", _FakePipeline)

    out = runner.invoke(
        app,
        ["rag", "retain", "--notebook", "finance", "--tenant", "default"],
    )
    assert out.exit_code == 0
    assert "Retention complete" in out.output
    assert "4 chunk(s)" in out.output


def test_knowledge_notebooks_create_and_update_backend_policy(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    _patch_data_dir(monkeypatch, tmp_path)

    create_out = runner.invoke(
        app,
        [
            "knowledge",
            "notebooks",
            "--create",
            "finance",
            "--tenant",
            "default",
            "--store-backend",
            "memory",
        ],
    )
    assert create_out.exit_code == 0
    assert "backend=memory" in create_out.output

    update_out = runner.invoke(
        app,
        [
            "knowledge",
            "notebooks",
            "--set-backend",
            "finance",
            "--tenant",
            "default",
            "--store-backend",
            "chroma",
        ],
    )
    assert update_out.exit_code == 0
    assert "backend=chroma" in update_out.output

    list_out = runner.invoke(app, ["knowledge", "list", "--tenant", "default"])
    assert list_out.exit_code == 0
    assert "finance" in list_out.output
    assert "chroma" in list_out.output


def test_tenant_create_and_update_backend_policy(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    _patch_data_dir(monkeypatch, tmp_path)

    create_out = runner.invoke(
        app,
        [
            "tenant",
            "create",
            "Acme",
            "--store-backend",
            "memory",
        ],
    )
    assert create_out.exit_code == 0
    assert "backend=memory" in create_out.output

    list_out = runner.invoke(app, ["tenant", "list"])
    assert list_out.exit_code == 0
    assert "Acme" in list_out.output
    assert "memory" in list_out.output

    from zen_claw.auth.tenant import TenantStore

    tenant_id = TenantStore(tmp_path).list()[0].tenant_id
    update_out = runner.invoke(
        app,
        [
            "tenant",
            "set-backend",
            tenant_id,
            "--store-backend",
            "chroma",
        ],
    )
    assert update_out.exit_code == 0
    assert "backend=chroma" in update_out.output
