# zen-claw · 小馋虾

[中文版](#中文版)

**中文名：小馋虾** — 本地优先的企业级 AI Agent 框架

zen-claw is a local-first AI agent framework for developers who need controllable tool execution, multi-agent orchestration, multi-channel delivery, and traceable operator workflows in one codebase.

## What This Project Is

zen-claw is not just a chat wrapper around an LLM. The current repository already combines:

- An agent runtime with direct-intent handling, tool invocation, approval-aware execution, and recovery-aware routing
- Multi-agent profile routing with isolated workspaces, route preview, sticky bindings, and profile-level model/prompt/tool controls
- A knowledge and RAG stack with notebook management, retention, tenant-aware storage policy, and operator-facing APIs
- A skills system with inventory, preflight, enable/disable, export/restore, and built-in business-skill foundations
- A FastAPI dashboard/control plane for agents, skills, ops summary, model routing, RAG, and crawler management
- Multi-channel integrations and webhook-style entrypoints for practical deployment scenarios

## Design Background

Several core design decisions in zen-claw are informed by recent quantization and attention research — specifically BitNet b1.58 and multi-head routing ideas. The goal is not to reproduce academic models, but to apply their structural intuitions (ternary classification, parallel candidate heads, gated structured output) to the practical problems of agent memory, intent routing, and execution control.

The design is structured as three layers:

### Layer 1 — b1.58 Retrieval (Implemented)

**Memory recall as ternary classification instead of top-k float ranking.**

Standard memory recall returns the top-N candidates by cosine similarity, which forces the agent to consume a fixed budget of context regardless of whether the candidates are actually relevant. Inspired by b1.58 ternary weights {-1, 0, +1}, zen-claw replaces the float-rank cutoff with a three-tier gate:

| Tier | Score range | Action |
|------|-------------|--------|
| Accept (+1) | ≥ 0.6 | Inject into context immediately |
| Uncertain (0) | 0.2 – 0.6 | Promote via secondary scoring pass |
| Reject (−1) | < 0.2 | Drop silently |

This is implemented as `TernaryRecallStrategy` in `zen_claw/agent/memory_ternary.py`, wired into `ContextBuilder` via `memory_recall_mode = "ternary"` in config.

### Layer 2 — MHA Routing (Planned)

**Intent candidate ranking as multi-head scoring instead of first-match priority.**

Gate 1 currently runs three candidate sources in sequence — crystallized pipeline, declarative intent, and native handler — and returns the first match. The planned extension is to score all three heads in parallel and combine their scores before selecting a candidate, analogous to multi-head attention aggregating from different representation subspaces.

Insertion point: `IntentRouter` candidate ranking + `RouteCandidate` scoring in `orchestration.py`.  
Risk: medium — requires offline routing eval data to validate ranking quality.

### Layer 3 — b1.58 Gating (Planned)

**Unified structured rejection output from the approval gate.**

Currently, `ApprovalGate` and `tool_policy.py` produce refusals through separate code paths with inconsistent reason fields. The planned work is to apply a ternary gate at the top of the execution decision: approve / uncertain (escalate) / reject, writing a normalized `RoutingDecision.reason` on every rejection so the audit trail and dashboard always see the same structured shape.

Insertion point: `ApprovalGate` rejection path → `RoutingDecision.reason`.  
Risk: low — mostly field standardization with no behavior change.

---

## Current Implemented Capability Areas

### Agent Runtime And Routing

- Direct-intent routes for time, weather, exchange rate, stock price, quick note, and fixed-site lookup
- Tool-calling runtime with approval-aware execution and structured recovery outcomes
- Gate 1/2/3 routing architecture:
  - Gate 1: rule-based candidate matching (crystallized, declarative, native handler)
  - Gate 2: lightweight LLM arbitration (confirm / select-skill / clarify / unclassified)
  - Gate 3: free planning via full agent loop
- Ternary memory recall (`TernaryRecallStrategy`) — b1.58-inspired three-tier context injection
- Multi-agent profile registry with profile-level workspace/model/planning overrides, prompt binding, tool allow/deny policy, route preview, route bind, and route clear

### Skills And Tools

- Structured skill inventory and loader/runtime integration
- Preflight and validation flows via `zen-claw skills test <name>`
- Skill enable/disable, export/restore, package policy, and batch operations
- Multi-source skill registry (GitHub, local directory, central HTTPS registry) with staging sandbox
- Built-in business-facing skill foundations: `content_gen`, `compliance_check`, `rag_retrieve`, `crawler`, `database_assistant`, `browser_automation`, `devops_monitor`, `knowledge_management`, `gdrive`

### Knowledge And RAG

- Shared `RAGPipeline` abstraction for ingest, search, stats, retention, and document lifecycle
- Notebook-level and tenant-level backend policy
- Metadata-aware ingestion and exact-match search filters
- Tenant-aware API and CLI surfaces for RAG operations
- Dashboard/API management for notebooks, repair, policy history, activity history, and backend diagnostics

### Dashboard And Control Plane APIs

- Dashboard cards and APIs for agents, skills, model routing, operations summary / pending apply, RAG, and crawler sources and schedules
- Representative API families: `/api/v1/agents`, `/api/v1/skills`, `/api/v1/rag`, `/api/v1/ops`, `/api/v1/model-routing`, `/api/v1/crawler`

### Channels, Webhooks, And Crawler

- Channel support: WebChat, Webhook Trigger, Slack, Signal, Matrix, Telegram, Discord, WhatsApp, Feishu, and others
- Webhook-style inbound paths and dashboard-operable channel controls
- Crawler: source catalog, run/schedule, browser-backed extraction, dashboard/API surfaces

---

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

Optional dependency groups:

- `pip install -e .[rag]` — Chroma / embeddings / document ingestion
- `pip install -e .[tts]` or `pip install -e .[tts-all]` — speech providers
- `pip install -e .[multitenant]` — multi-tenant auth extras

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
- The Python mainline is the repository's Alpha-grade baseline. Lower-maturity areas are mostly optional Node sidecars such as `bridge/` and `browser/sidecar/`, plus documentation consistency work.
- Node-side installs are expected to use committed lockfiles and controlled version updates. Unlocked `npm install` paths are not part of the release-grade baseline.
- `zen_claw/dashboard/static/chat.html` still consumes pinned CDN assets; treat those as a bounded residual supply-chain dependency unless vendored locally.
- The repository includes roadmap and audit documents; README describes the current implemented baseline only.
- Verification commands and repo structure should always defer to the generated docs in `docs/`.

## Developer Navigation

- Project overview: [`docs/project-overview.md`](docs/project-overview.md)
- Repository map: [`docs/repo_map.md`](docs/repo_map.md)
- Verification source of truth: [`docs/verify_profile.md`](docs/verify_profile.md)
- Deployment guide: [`docs/DEPLOY.md`](docs/DEPLOY.md)
- WhatsApp bridge runbook: [`bridge/README.md`](bridge/README.md)
- Feature usage reference: [`docs/feature-summary_and_usage.md`](docs/feature-summary_and_usage.md)
- Self-test and playbook: [`docs/self-test-and-playbook.md`](docs/self-test-and-playbook.md)
- Product/roadmap context: [`docs/zen-claw功能补足开发计划.md`](docs/zen-claw功能补足开发计划.md)

## Development Verification

Use [`docs/verify_profile.md`](docs/verify_profile.md) as the source of truth. Current baseline:

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -q
```

## License

BUSL-1.1 (Business Source License 1.1)

Commercial use is restricted until 2030-04-01, after which this software is available under Apache 2.0.
During the restriction period, use is permitted for evaluation, testing, and non-production purposes.

---

<span id="中文版"></span>
# 中文版

zen-claw 是一个面向开发者的本地优先 AI Agent 框架，目标是在同一套代码里提供可控工具执行、多 Agent 编排、多渠道接入，以及可追踪的运维控制面。

## 这个项目现在是什么

它不是一个单纯的 LLM 聊天壳。当前仓库已经把这些能力落在同一个系统里：

- Agent 运行时：直达意图、工具调用、审批感知执行、结构化恢复路径
- 多 Agent 配置与路由：隔离工作区、route preview、sticky route、profile 级模型/Prompt/工具控制
- Knowledge / RAG：notebook 管理、保留策略、租户级后端策略、操作面 API
- Skills 系统：清单、preflight、启停、导出恢复、多源注册表与 staging 沙箱、内置业务技能骨架
- Dashboard / FastAPI 控制面：agents、skills、ops、model routing、RAG、crawler
- 多渠道与 webhook/crawler 等实际交付需要的外围能力

## 设计背景

zen-claw 的几个核心设计决策来自近期量化与注意力机制研究的启发——主要是 BitNet b1.58 和多头路由的思路。目标不是复现学术模型，而是把这些结构直觉（三态分类、平行候选头、门控结构化输出）用于解决 Agent 记忆、意图路由和执行控制这三个实际问题。

整体设计分为三层：

### 第一层 — b1.58 检索层（已实现）

**用三态分类替代 top-k 浮点排名来做记忆召回。**

传统记忆召回按余弦相似度取前 N 条，无论候选是否真正相关都会消耗固定的上下文预算。借鉴 b1.58 三值权重 {-1, 0, +1} 的思路，zen-claw 用三态门控取代浮点截断：

| 层级 | 分数范围 | 动作 |
|------|----------|------|
| 接受 (+1) | ≥ 0.6 | 直接注入上下文 |
| 不确定 (0) | 0.2 – 0.6 | 二次评分后决定升降 |
| 拒绝 (−1) | < 0.2 | 静默丢弃 |

已实现为 `TernaryRecallStrategy`，位于 `zen_claw/agent/memory_ternary.py`，通过配置项 `memory_recall_mode = "ternary"` 接入 `ContextBuilder`。

### 第二层 — MHA 规划层（规划中）

**用多头并行评分替代顺序首次匹配来做意图候选排序。**

当前 Gate 1 按顺序跑三个候选源（crystallized pipeline → declarative intent → native handler），取第一个匹配。规划中的扩展是对三个头并行打分、合并后再选出候选，类似多头注意力从不同子空间聚合表示的方式。

插入点：`IntentRouter` 候选排序 + `orchestration.py` 中的 `RouteCandidate` 评分。  
风险：中——需要离线路由评测数据来验证排序质量。

### 第三层 — b1.58 顶层门控（规划中）

**从审批门统一输出结构化拒绝结果。**

当前 `ApprovalGate` 和 `tool_policy.py` 通过不同代码路径产生拒绝，reason 字段格式不统一。规划中的工作是在执行决策入口加一个三态门控：批准 / 不确定（上报）/ 拒绝，所有拒绝路径统一写入标准化的 `RoutingDecision.reason`，保证审计日志和 Dashboard 始终看到一致的结构。

插入点：`ApprovalGate` 拒绝路径 → `RoutingDecision.reason`。  
风险：低——主要是字段标准化，不改变现有行为。

---

## 当前已实现的能力分区

### Agent Runtime 与路由

- 直达意图处理：时间、天气、汇率、股票行情、快速备忘、固定站点查询
- 支持工具调用、审批约束、恢复结果建模的 Agent 运行时
- Gate 1/2/3 三级路由架构：
  - Gate 1：规则匹配（crystallized、declarative、native handler 三通道）
  - Gate 2：轻量 LLM 仲裁（confirm / select-skill / clarify / unclassified）
  - Gate 3：完整 Agent Loop 自由规划
- 三态记忆召回（`TernaryRecallStrategy`）——b1.58 启发的三层上下文注入
- 多 Agent profile 注册与路由，含 profile 级 workspace/model/planning 覆盖、prompt 绑定、工具 allow/deny 策略、route preview、route bind、route clear

### Skills 与工具

- 结构化技能清单与运行时绑定
- `zen-claw skills test <name>` preflight / 测试入口
- 技能启停、导出恢复、package policy、批量操作
- 多源技能注册表（GitHub、本地目录、中央 HTTPS registry）+ staging 沙箱
- 内置业务技能骨架：`content_gen`、`compliance_check`、`rag_retrieve`、`crawler`、`database_assistant`、`browser_automation`、`devops_monitor`、`knowledge_management`、`gdrive`

### Knowledge 与 RAG

- 统一的 `RAGPipeline` 抽象，覆盖 ingest、search、stats、retention、document lifecycle
- notebook 级与 tenant 级后端策略
- 支持 metadata 注入与精确过滤搜索
- 提供 tenant-aware 的 CLI 与 API 入口
- Dashboard/API 支持 notebook、repair、policy history、activity history、backend diagnostics

### Dashboard 与控制面 API

- 当前 Dashboard 与 API 已覆盖：agents、skills、model routing、operations summary / pending apply、RAG、crawler source / schedule
- 代表性 API 家族：`/api/v1/agents`、`/api/v1/skills`、`/api/v1/rag`、`/api/v1/ops`、`/api/v1/model-routing`、`/api/v1/crawler`

### 渠道、Webhook 与 Crawler

- 仓库内已包含 WebChat、Webhook Trigger、Slack、Signal、Matrix、Telegram、Discord、WhatsApp、Feishu 等渠道支持
- 现有代码中已存在 webhook 风格的入站路径与 Dashboard 可操作的控制面
- crawler 能力：source catalog、run/schedule、browser-backed extraction、Dashboard/API 面板

---

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
- 仓库里存在 roadmap 与 audit 文档；README 只描述"当前已实现基线"，不把历史规划自动视为完成。
- 涉及验证命令和仓库结构时，应以 `docs/` 中生成的真源文档为准。

## 开发者导航

- 项目全景说明：[`docs/project-overview.md`](docs/project-overview.md)
- 仓库地图：[`docs/repo_map.md`](docs/repo_map.md)
- 验证真源：[`docs/verify_profile.md`](docs/verify_profile.md)
- 部署说明：[`docs/DEPLOY.md`](docs/DEPLOY.md)
- 自测策略与玩法：[`docs/self-test-and-playbook.md`](docs/self-test-and-playbook.md)
- 功能与用法参考：[`docs/feature-summary_and_usage.md`](docs/feature-summary_and_usage.md)
- 产品/路线图上下文：[`docs/zen-claw功能补足开发计划.md`](docs/zen-claw功能补足开发计划.md)

## 开发验证

以 [`docs/verify_profile.md`](docs/verify_profile.md) 为准，当前基线：

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -q
```

## 许可证

BUSL-1.1（Business Source License 1.1）

商业限制期至 2030-04-01，到期后自动转为 Apache 2.0。
限制期内允许用于评估、测试及非生产目的。
