"""Notion tool — read/write pages and databases via official notion-client SDK."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult

if TYPE_CHECKING:
    from zen_claw.auth.credentials import CredentialVault
    from zen_claw.config.schema import NotionConfig


def _blocks_to_text(blocks: list[dict[str, Any]], depth: int = 0) -> str:
    """Convert Notion block list to readable plain text."""
    lines = []
    indent = "  " * depth
    for block in blocks:
        btype = block.get("type", "")
        data = block.get(btype, {})
        rich = data.get("rich_text", [])
        text = "".join(r.get("plain_text", "") for r in rich)

        if btype in ("paragraph", "quote"):
            if text:
                lines.append(f"{indent}{text}")
        elif btype in ("heading_1", "heading_2", "heading_3"):
            level = int(btype[-1])
            prefix = "#" * level
            lines.append(f"{indent}{prefix} {text}")
        elif btype in ("bulleted_list_item", "to_do"):
            checked = data.get("checked", False)
            marker = "[x]" if checked else "[ ]" if btype == "to_do" else "-"
            lines.append(f"{indent}{marker} {text}")
        elif btype == "numbered_list_item":
            lines.append(f"{indent}1. {text}")
        elif btype == "code":
            lang = data.get("language", "")
            lines.append(f"{indent}```{lang}\n{indent}{text}\n{indent}```")
        elif btype == "divider":
            lines.append(f"{indent}---")
        elif btype == "child_page":
            title = data.get("title", "")
            lines.append(f"{indent}[Page: {title}]")
        elif text:
            lines.append(f"{indent}{text}")

        # Recurse into children if present
        children = block.get("children", [])
        if children:
            lines.append(_blocks_to_text(children, depth + 1))

    return "\n".join(lines)


def _content_to_blocks(content: str) -> list[dict[str, Any]]:
    """Convert plain text content to Notion paragraph blocks."""
    blocks = []
    for line in content.splitlines():
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": line}}]
            },
        })
    return blocks or [{
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": []},
    }]


class NotionTool(Tool):
    """Interact with Notion pages, databases and search."""

    name = "notion"
    description = (
        "Interact with Notion: search pages/databases, read page content, create or update pages, "
        "query database records, create database items. Requires notion-client installed and token configured."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "search",
                    "read_page",
                    "create_page",
                    "update_page",
                    "query_database",
                    "create_database_item",
                ],
                "description": "Action to perform.",
            },
            "query": {"type": "string", "description": "Search query (for search)."},
            "page_id": {"type": "string", "description": "Notion page or block ID."},
            "database_id": {"type": "string", "description": "Notion database ID."},
            "parent_id": {
                "type": "string",
                "description": "Parent page or database ID for new page.",
            },
            "title": {"type": "string", "description": "Page title (for create_page)."},
            "content": {
                "type": "string",
                "description": "Text content for page body (plain text, line-per-paragraph).",
            },
            "properties": {
                "type": "object",
                "description": "Notion properties dict (for create_database_item or update_page).",
            },
            "filter": {
                "type": "object",
                "description": "Notion filter object (for query_database).",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Max results to return (default 20).",
            },
        },
        "required": ["action"],
    }

    def __init__(self, config: "NotionConfig", vault: "CredentialVault | None" = None):
        self._cfg = config
        self._vault = vault
        self._client: Any = None

    def _get_token(self) -> str | None:
        if self._vault is None:
            from zen_claw.auth.credentials import CredentialVault
            self._vault = CredentialVault()
        return self._vault.get(self._cfg.credential_platform, self._cfg.credential_key)

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from notion_client import Client  # type: ignore[import-untyped]
            except ImportError:
                raise RuntimeError(
                    "notion-client is not installed. Run: pip install 'zen-claw-ai[notion]'"
                )
            token = self._get_token()
            if not token:
                raise RuntimeError(
                    "Notion token not found. Run: zen-claw credentials set notion token <YOUR_TOKEN>"
                )
            self._client = Client(auth=token)
        return self._client

    def _check_config(self) -> str | None:
        if not self._cfg.enabled:
            return "Notion tool is not enabled. Set tools.notion.enabled = true in config."
        return None

    # ── Sync operations (wrapped with to_thread) ───────────────────────────────

    def _search(self, query: str, limit: int) -> str:
        client = self._get_client()
        resp = client.search(query=query, page_size=limit)
        results = resp.get("results", [])
        if not results:
            return f"No Notion results found for: {query!r}"
        lines = []
        for item in results:
            obj_type = item.get("object", "")
            item_id = item.get("id", "")
            if obj_type == "page":
                props = item.get("properties", {})
                title_prop = props.get("title") or props.get("Name") or {}
                rich = title_prop.get("title", title_prop.get("rich_text", []))
                title = "".join(r.get("plain_text", "") for r in rich) or "(untitled)"
                lines.append(f"Page  id={item_id}  title={title!r}")
            elif obj_type == "database":
                title_arr = item.get("title", [])
                title = "".join(r.get("plain_text", "") for r in title_arr) or "(untitled)"
                lines.append(f"DB    id={item_id}  title={title!r}")
        return "\n".join(lines)

    def _read_page(self, page_id: str) -> str:
        client = self._get_client()
        # Fetch block children
        resp = client.blocks.children.list(block_id=page_id, page_size=100)
        blocks = resp.get("results", [])
        if not blocks:
            return "(empty page)"
        return _blocks_to_text(blocks)

    def _create_page(self, parent_id: str, title: str, content: str) -> str:
        client = self._get_client()
        parent: dict[str, Any]
        # Determine parent type: try as database first, fall back to page
        try:
            client.databases.retrieve(database_id=parent_id)
            parent = {"database_id": parent_id}
            properties: dict[str, Any] = {
                "Name": {"title": [{"type": "text", "text": {"content": title}}]}
            }
        except Exception:
            parent = {"page_id": parent_id}
            properties = {
                "title": {"title": [{"type": "text", "text": {"content": title}}]}
            }
        children = _content_to_blocks(content) if content else []
        page = client.pages.create(parent=parent, properties=properties, children=children)
        return f"Created page id={page['id']}  title={title!r}"

    def _update_page(self, page_id: str, properties: dict[str, Any] | None, content: str | None) -> str:
        client = self._get_client()
        msgs = []
        if properties:
            client.pages.update(page_id=page_id, properties=properties)
            msgs.append("properties updated")
        if content:
            blocks = _content_to_blocks(content)
            client.blocks.children.append(block_id=page_id, children=blocks)
            msgs.append("content appended")
        return f"Page {page_id}: {', '.join(msgs) or 'no changes'}"

    def _query_database(self, database_id: str, filter_obj: dict[str, Any] | None, limit: int) -> str:
        client = self._get_client()
        kwargs: dict[str, Any] = {"database_id": database_id, "page_size": limit}
        if filter_obj:
            kwargs["filter"] = filter_obj
        resp = client.databases.query(**kwargs)
        results = resp.get("results", [])
        if not results:
            return "No records found."
        lines = []
        for item in results:
            item_id = item.get("id", "")
            props = item.get("properties", {})
            # Grab the first title-type property
            title = "(untitled)"
            for _k, v in props.items():
                ptype = v.get("type", "")
                if ptype == "title":
                    rich = v.get("title", [])
                    title = "".join(r.get("plain_text", "") for r in rich) or title
                    break
            lines.append(f"id={item_id}  {title}")
        total = resp.get("has_more", False)
        suffix = " (more available)" if total else ""
        return "\n".join(lines) + suffix

    def _create_database_item(self, database_id: str, properties: dict[str, Any]) -> str:
        client = self._get_client()
        page = client.pages.create(
            parent={"database_id": database_id},
            properties=properties,
        )
        return f"Created database item id={page['id']}"

    # ── Public execute ─────────────────────────────────────────────────────────

    async def execute(self, action: str, **kwargs: Any) -> ToolResult:
        err = self._check_config()
        if err:
            return ToolResult.failure(ToolErrorKind.PERMISSION, err, code="notion_not_configured")

        try:
            limit = int(kwargs.get("limit") or 20)

            if action == "search":
                query = str(kwargs.get("query") or "").strip()
                if not query:
                    return ToolResult.failure(
                        ToolErrorKind.PARAMETER, "query is required", code="notion_missing_query"
                    )
                result = await asyncio.to_thread(self._search, query, limit)
                return ToolResult.success(result)

            elif action == "read_page":
                page_id = str(kwargs.get("page_id") or "").strip()
                if not page_id:
                    return ToolResult.failure(
                        ToolErrorKind.PARAMETER, "page_id is required", code="notion_missing_page_id"
                    )
                result = await asyncio.to_thread(self._read_page, page_id)
                return ToolResult.success(result)

            elif action == "create_page":
                parent_id = str(kwargs.get("parent_id") or "").strip()
                title = str(kwargs.get("title") or "Untitled").strip()
                content = str(kwargs.get("content") or "").strip()
                if not parent_id:
                    return ToolResult.failure(
                        ToolErrorKind.PARAMETER, "parent_id is required", code="notion_missing_parent"
                    )
                result = await asyncio.to_thread(self._create_page, parent_id, title, content)
                return ToolResult.success(result)

            elif action == "update_page":
                page_id = str(kwargs.get("page_id") or "").strip()
                if not page_id:
                    return ToolResult.failure(
                        ToolErrorKind.PARAMETER, "page_id is required", code="notion_missing_page_id"
                    )
                properties = kwargs.get("properties")
                content = kwargs.get("content")
                result = await asyncio.to_thread(
                    self._update_page, page_id,
                    properties if isinstance(properties, dict) else None,
                    str(content) if content else None,
                )
                return ToolResult.success(result)

            elif action == "query_database":
                db_id = str(kwargs.get("database_id") or "").strip()
                if not db_id:
                    return ToolResult.failure(
                        ToolErrorKind.PARAMETER, "database_id is required", code="notion_missing_db_id"
                    )
                filter_obj = kwargs.get("filter")
                result = await asyncio.to_thread(
                    self._query_database, db_id,
                    filter_obj if isinstance(filter_obj, dict) else None,
                    limit,
                )
                return ToolResult.success(result)

            elif action == "create_database_item":
                db_id = str(kwargs.get("database_id") or "").strip()
                properties = kwargs.get("properties")
                if not db_id:
                    return ToolResult.failure(
                        ToolErrorKind.PARAMETER, "database_id is required", code="notion_missing_db_id"
                    )
                if not isinstance(properties, dict):
                    return ToolResult.failure(
                        ToolErrorKind.PARAMETER, "properties dict is required", code="notion_missing_properties"
                    )
                result = await asyncio.to_thread(self._create_database_item, db_id, properties)
                return ToolResult.success(result)

            else:
                return ToolResult.failure(
                    ToolErrorKind.PARAMETER,
                    f"Unknown action: {action!r}",
                    code="notion_unknown_action",
                )

        except RuntimeError as exc:
            # Missing dep or token
            return ToolResult.failure(ToolErrorKind.PERMISSION, str(exc), code="notion_setup_error")
        except Exception as exc:
            return ToolResult.failure(ToolErrorKind.RUNTIME, f"Notion error: {exc}", code="notion_error")
