from pathlib import Path
from unittest.mock import MagicMock, patch

from zen_claw.auth.paths import tenant_data_dir
from zen_claw.knowledge.notebook import NotebookManager
from zen_claw.knowledge.retriever import HybridSearchResult
from zen_claw.skills.rag_retrieve.retriever import RAGRetrieverSkill


def test_rag_skill_retrieves_grounded_results(tmp_path: Path) -> None:
    manager = NotebookManager(tenant_data_dir(tmp_path, "default"))
    manager.create("kb")
    with patch("zen_claw.knowledge.retriever.HybridRetriever.from_notebook") as mock_factory:
        mock_retriever = MagicMock()
        mock_retriever.search.return_value = [
            HybridSearchResult(
                content="Policy text",
                source="policy.md",
                score=0.9,
                page=1,
                rrf_score=0.95,
            )
        ]
        mock_factory.return_value = mock_retriever
        skill = RAGRetrieverSkill(tmp_path, default_notebook="kb")
        result = skill.retrieve("policy", notebook_id="kb", top_k=3)

    assert result["notebook"] == "kb"
    assert result["notebook_id"] == "kb"
    assert result["tenant_id"] == "default"
    assert result["results"][0]["source"] == "policy.md"
