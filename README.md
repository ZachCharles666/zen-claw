# zen-claw

[中文版](#中文版)

zen-claw is a local-first AI agent framework for developers who need controllable tool execution, multi-agent orchestration, multi-channel delivery, and traceable operator workflows in one codebase.

## What This Project Is

zen-claw is not just a chat wrapper around an LLM. The current repository already combines:

- An agent runtime with direct-intent handling, tool invocation, approval-aware execution, and recovery-aware routing
- Multi-agent profile routing with isolated workspaces, route preview, sticky bindings, and profile-level model/prompt/tool controls
- A knowledge and RAG stack with notebook management, retention, tenant-aware storage policy, and operator-facing APIs
- A skills system with inventory, preflight, enable/disable, export/restore, and built-in business-skill foundations
- A FastAPI dashboard/control plane for agents, skills, ops summary, model routing, RAG, and crawler management
- Multi-channel integrations and webhook-style entrypoints for practical deployment scenarios

## Current Implemented Capability Areas

### Agent Runtime And Routing

- Direct-intent routes for common deterministic tasks such as time, weather, exchange, and fixed-site lookup
- Tool-calling runtime with approval-aware execution and structured recovery outcomes
- Multi-agent profile registry with:
  - profile-level workspace/model/planning overrides
  - prompt binding and tool allow/deny policy
  - route preview, route bind, and route clear flows
- Operator visibility through CLI, dashboard, and `/api/v1/agents*` endpoints

### Skills And Tools

- Structured skill inventory and loader/runtime integration
- Preflight and validation flows via `zen-claw skills test <name>`
- Skill enable/disable, export/restore, package policy, and batch operations
- Built-in business-facing skill foundations including `content_gen`, `compliance_check`, `rag_retrieve`, and crawler-related flows

### Knowledge And RAG

- Shared `RAGPipeline` abstraction for ingest, search, stats, retention, and document lifecycle
- Notebook-level and tenant-level backend policy
- Metadata-aware ingestion and exact-match search filters
- Tenant-aware API and CLI surfaces for RAG operations
- Dashboard/API management for notebooks, repair, policy history, activity history, and backend diagnostics

### Dashboard And Control Plane APIs

- Dashboard cards and APIs for:
  - agents
  - skills
  - model routing
  - operations summary / pending apply
  - RAG
  - crawler sources and schedules
- Representative API families:
  - `/api/v1/agents`
  - `/api/v1/skills`
  - `/api/v1/rag`
  - `/api/v1/ops`
  - `/api/v1/model-routing`
  - `/api/v1/crawler`

### Channels, Webhooks, And Crawler

- Channel support in the repository includes WebChat, Webhook Trigger, Slack, Signal, Matrix, Telegram, Discord, WhatsApp, Feishu, and others
- Webhook-style inbound paths and dashboard-operable channel controls exist in the current codebase
- Crawler support includes source catalog, run/schedule flows, browser-backed extraction, and dashboard/API surfaces

## Developer Quick Start

### 1. Install

```powershell
git clone https://github.com/ZachCharles666/zen-claw.git
cd zen-claw
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e .[dev]
```

Optional dependency groups are available when you need them:

- `pip install -e .[rag]` for Chroma / embeddings / document ingestion extras
- `pip install -e .[tts]` or `pip install -e .[tts-all]` for speech-related providers
- `pip install -e .[multitenant]` for multi-tenant auth-related extras

### 2. Configure

```powershell
zen-claw config wizard
zen-claw config doctor --strict
```

The wizard writes the active config to `~/.zen-claw/config.json`. This path is global for the current user and does not change with your working directory.

### 3. Run A Local Agent Prompt

```powershell
zen-claw agent -m "Hello"
```

### 4. Start Gateway And Dashboard

```powershell
zen-claw gateway --port 18790
zen-claw dashboard --host 127.0.0.1 --port 18791
```

Dashboard URL: [http://127.0.0.1:18791](http://127.0.0.1:18791)

## Common CLI Entry Points

```powershell
zen-claw status -v
zen-claw config providers
zen-claw config troubleshoot
zen-claw agent list
zen-claw agent chat default
zen-claw skills test content_gen
zen-claw rag stats
zen-claw crawler run --source https://example.com --notebook demo
```

## Repository Shape

These top-level areas are the main places to read code:

- `zen_claw/agent/`: agent loop, routing, pool, tool selection, runtime decisions
- `zen_claw/skills/`: built-in skills, manifests, runtime skill integration
- `zen_claw/knowledge/`: ingestion, notebooks, retrieval, vector-store abstraction, RAG pipeline
- `zen_claw/dashboard/`: dashboard server and operator-facing API surface
- `zen_claw/channels/`: inbound/outbound channel integrations
- `zen_claw/cron/`: scheduled execution and job orchestration
- `zen_claw/tunnel/`, `bridge/`, `browser/sidecar/`, `go/`: supporting runtime sidecars and infra helpers
- `tests/`: behavior and regression coverage across runtime, CLI, dashboard, RAG, channels, and crawler flows

## Current Boundaries And Notes

- README only covers stable entry points and repository-level orientation. It does not try to replace subsystem-specific docs.
- Some features depend on optional extras, external credentials, or channel/provider-specific runtime setup.
- The repository includes roadmap and audit documents; README should be read as “current implemented baseline”, not as a promise that every historical plan item is complete.
- Verification commands and repo structure should always defer to the generated docs in `docs/`.

## Developer Navigation

- Project overview: [`docs/project-overview.md`](docs/project-overview.md)
- Repository map: [`docs/repo_map.md`](docs/repo_map.md)
- Verification source of truth: [`docs/verify_profile.md`](docs/verify_profile.md)
- Deployment guide: [`docs/DEPLOY.md`](docs/DEPLOY.md)
- Feature usage reference: [`docs/feature-summary_and_usage.md`](docs/feature-summary_and_usage.md)
- Product/roadmap context: [`docs/zen-claw功能补足开发计划.md`](docs/zen-claw功能补足开发计划.md)

## Development Verification

Use [`docs/verify_profile.md`](docs/verify_profile.md) as the source of truth. Current baseline:

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -q
```

## License

MIT

---

<span id="中文版"></span>
# 中文版

zen-claw 是一个面向开发者的本地优先 AI Agent 框架，目标是在同一套代码里提供可控工具执行、多 Agent 编排、多渠道接入，以及可追踪的运维控制面。

## 这个项目现在是什么

它不是一个单纯的 LLM 聊天壳。当前仓库已经把这些能力落在同一个系统里：

- Agent 运行时：直达意图、工具调用、审批感知执行、结构化恢复路径
- 多 Agent 配置与路由：隔离工作区、route preview、sticky route、profile 级模型/Prompt/工具控制
- Knowledge / RAG：notebook 管理、保留策略、租户级后端策略、操作面 API
- Skills 系统：清单、preflight、启停、导出恢复、内置业务技能骨架
- Dashboard / FastAPI 控制面：agents、skills、ops、model routing、RAG、crawler
- 多渠道与 webhook/crawler 等实际交付需要的外围能力

## 当前已实现的能力分区

### Agent Runtime 与路由

- 针对时间、天气、汇率、固定站点查询等场景的直达意图处理
- 支持工具调用、审批约束、恢复结果建模的 Agent 运行时
- 多 Agent profile 注册与路由能力，包括：
  - profile 级 workspace / model / planning 覆盖
  - prompt 绑定与工具 allow/deny 策略
  - route preview、route bind、route clear
- 可通过 CLI、Dashboard 与 `/api/v1/agents*` 观察和管理

### Skills 与工具

- 结构化技能清单与运行时绑定
- `zen-claw skills test <name>` preflight / 测试入口
- 技能启停、导出恢复、package policy、批量操作
- 当前仓库已有业务向技能基础能力，例如 `content_gen`、`compliance_check`、`rag_retrieve` 与 crawler 相关能力

### Knowledge 与 RAG

- 统一的 `RAGPipeline` 抽象，覆盖 ingest、search、stats、retention、document lifecycle
- notebook 级与 tenant 级后端策略
- 支持 metadata 注入与精确过滤搜索
- 提供 tenant-aware 的 CLI 与 API 入口
- Dashboard/API 支持 notebook、repair、policy history、activity history、backend diagnostics

### Dashboard 与控制面 API

- 当前 Dashboard 与 API 已覆盖：
  - agents
  - skills
  - model routing
  - operations summary / pending apply
  - RAG
  - crawler source / schedule
- 代表性 API 家族：
  - `/api/v1/agents`
  - `/api/v1/skills`
  - `/api/v1/rag`
  - `/api/v1/ops`
  - `/api/v1/model-routing`
  - `/api/v1/crawler`

### 渠道、Webhook 与 Crawler

- 仓库内已包含 WebChat、Webhook Trigger、Slack、Signal、Matrix、Telegram、Discord、WhatsApp、Feishu 等渠道支持
- 现有代码中已存在 webhook 风格的入站路径与 Dashboard 可操作的控制面
- crawler 能力已覆盖 source catalog、run/schedule、browser-backed extraction、Dashboard/API 面板

## 开发者快速开始

### 1. 安装

```powershell
git clone https://github.com/ZachCharles666/zen-claw.git
cd zen-claw
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e .[dev]
```

按需安装可选依赖：

- `pip install -e .[rag]`：Chroma / embedding / 文档解析相关依赖
- `pip install -e .[tts]` 或 `pip install -e .[tts-all]`：语音相关能力
- `pip install -e .[multitenant]`：多租户认证相关能力

### 2. 初始化配置

```powershell
zen-claw config wizard
zen-claw config doctor --strict
```

向导会把当前生效配置写入 `~/.zen-claw/config.json`。这是当前用户的全局配置路径，不会随着你切换工作目录而改变。

### 3. 本地运行一个 Agent Prompt

```powershell
zen-claw agent -m "Hello"
```

### 4. 启动 Gateway 与 Dashboard

```powershell
zen-claw gateway --port 18790
zen-claw dashboard --host 127.0.0.1 --port 18791
```

访问地址：[http://127.0.0.1:18791](http://127.0.0.1:18791)

## 常用 CLI 入口

```powershell
zen-claw status -v
zen-claw config providers
zen-claw config troubleshoot
zen-claw agent list
zen-claw agent chat default
zen-claw skills test content_gen
zen-claw rag stats
zen-claw crawler run --source https://example.com --notebook demo
```

## 仓库结构怎么读

如果你要快速建立代码全景，优先看这些目录：

- `zen_claw/agent/`：agent loop、intent routing、pool、tool 选择、运行时决策
- `zen_claw/skills/`：内置技能、manifest、skill 运行时集成
- `zen_claw/knowledge/`：ingestion、notebook、retrieval、vector store 抽象、RAG pipeline
- `zen_claw/dashboard/`：dashboard server 与 operator-facing API
- `zen_claw/channels/`：各渠道接入
- `zen_claw/cron/`：定时任务与调度
- `zen_claw/tunnel/`、`bridge/`、`browser/sidecar/`、`go/`：配套 sidecar 和基础设施辅助组件
- `tests/`：覆盖 runtime、CLI、dashboard、RAG、channels、crawler 的回归测试

## 当前边界与注意事项

- README 只覆盖稳定入口与仓库导航，不展开所有子系统细节。
- 部分能力依赖可选 extras、外部凭证或渠道/模型特定配置。
- 仓库里存在 roadmap 与 audit 文档；README 只描述“当前已实现基线”，不把历史规划自动视为完成。
- 涉及验证命令和仓库结构时，应以 `docs/` 中生成的真源文档为准。

## 开发者导航

- 项目全景说明：[`docs/project-overview.md`](docs/project-overview.md)
- 仓库地图：[`docs/repo_map.md`](docs/repo_map.md)
- 验证真源：[`docs/verify_profile.md`](docs/verify_profile.md)
- 部署说明：[`docs/DEPLOY.md`](docs/DEPLOY.md)
- 功能与用法参考：[`docs/feature-summary_and_usage.md`](docs/feature-summary_and_usage.md)
- 产品/路线图上下文：[`docs/zen-claw功能补足开发计划.md`](docs/zen-claw功能补足开发计划.md)

## 开发验证

以 [`docs/verify_profile.md`](docs/verify_profile.md) 为准，当前基线：

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -q
```

## 许可证

MIT
