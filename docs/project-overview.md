# zen-claw 项目说明

本文档面向开发者，总结当前 `zen-claw` 仓库已经落地的能力、主要模块、核心运行链路、控制面、以及阅读代码时最重要的入口。它的目标不是替代每个子系统的专门设计文档，而是提供一份“进入仓库后的全景图”。

## 1. 项目定位

`zen-claw` 是一个本地优先的 AI Agent 框架。它不是单一的聊天封装，而是一套把以下能力放在同一代码库中的运行平台：

- Agent 运行时与工具调用
- 多 Agent 配置、路由、隔离工作区
- Skills / tools 生命周期
- Knowledge / RAG 管线
- Dashboard / FastAPI 控制面
- 多渠道接入与 webhook 风格入口
- 定时任务、crawler、审计和运维可视化

如果用一句话概括：`zen-claw` 现在更接近“可运营的 Agent 平台骨架”，而不是“一个只会对话的 LLM Demo”。

## 2. 当前仓库里的核心能力

### 2.1 Agent Runtime

当前运行时已经具备：

- Agent loop
- 会话上下文与记忆抽取
- 工具调用与结果回注
- 审批感知执行
- 结构化恢复与 fallback 路径
- 直达意图处理

其中直达意图已经覆盖了一些典型确定性任务，例如：

- 时间 / 时区查询
- 天气查询
- 汇率查询
- 固定站点摘要类查询

这部分代码主要集中在：

- `zen_claw/agent/loop.py`
- `zen_claw/agent/intent_router.py`
- `zen_claw/agent/tools/`

### 2.2 多 Agent 编排

仓库当前已经不是单一 Agent 结构。已有基础能力包括：

- profile 注册与配置解析
- 每个 profile 独立 workspace
- profile 级 model / planning / prompt / tools 控制
- route preview
- sticky route
- channel default agentProfile

控制面已经能看到并操作其中一部分：

- `GET /api/v1/agents`
- `GET /api/v1/agents/{id}`
- `POST /api/v1/agents/route-preview`
- planning / model / routing keyword 等变更入口

相关代码主要在：

- `zen_claw/agent/`
- `zen_claw/config/`
- `zen_claw/dashboard/server.py`

### 2.3 Skills 与工具层

当前仓库已有技能体系，不再只是“硬编码工具集合”：

- 结构化 skill inventory
- preflight / 测试入口
- enable / disable
- export / restore
- package policy
- batch 操作

当前内置的业务向技能基础包括：

- `content_gen`
- `compliance_check`
- `rag_retrieve`
- `crawler`

这意味着仓库已经具备了“技能作为产品能力单元”去管理，而不只是“在 agent loop 里直接塞工具”。

主要位置：

- `zen_claw/skills/`
- `zen_claw/agent/skills.py`
- `zen_claw/dashboard/server.py`

### 2.4 Knowledge / RAG

当前的 RAG 能力已经形成了统一抽象，而不是零散实验代码：

- `RAGPipeline`
- notebook 管理
- document lifecycle
- retention
- backend abstraction
- tenant-aware policy
- notebook / tenant backend policy
- activity / policy history
- dashboard / API 管理面

CLI、API、Dashboard 三条面都已经接到这套抽象上。

主要位置：

- `zen_claw/knowledge/`
- `zen_claw/agent/tools/knowledge.py`
- `zen_claw/dashboard/server.py`

### 2.5 Dashboard 与控制面

当前 Dashboard 已经不只是只读监控页，而是一个初步的 operator control plane。现有能力包括：

- agent inventory / detail / route preview
- skill inventory / detail / preflight / export / restore
- model routing summary / history
- operations summary / pending apply
- RAG notebook / policy / history
- crawler source / run / schedule

主控制面入口在：

- `zen_claw/dashboard/server.py`

代表性 API 家族：

- `/api/v1/agents`
- `/api/v1/skills`
- `/api/v1/rag`
- `/api/v1/ops`
- `/api/v1/model-routing`
- `/api/v1/crawler`

### 2.6 Channels、Webhook 与 Crawler

仓库已包含多个接入面，不是单一 CLI 应用：

- WebChat
- Webhook Trigger
- Slack
- Signal
- Matrix
- Telegram
- Discord
- WhatsApp
- Feishu

另外 crawler 不是独立脚本，而是已经和以下能力打通：

- source catalog
- run / schedule
- browser-backed extraction
- RAG ingest
- dashboard / API
- cron payload / execution

## 3. 主要运行链路

从高层看，当前项目的典型运行链路可以概括为下面几条。

### 3.1 入站消息到 Agent 回复

1. 某个渠道或 CLI 接收到输入
2. 输入被转换成统一消息结构并送入运行时
3. Agent runtime 先尝试直达意图或路由判断
4. 如需调用工具，则执行工具、审批或恢复路径
5. 结果回写到会话 / trace / observability
6. 最终回复经对应渠道发回

### 3.2 Agent 路由链路

当前路由优先级大致是：

- 显式 agent id
- 已绑定 sticky route
- profile keyword match
- channel default agentProfile
- fallback `default`

### 3.3 Knowledge / RAG 链路

1. 文档经 `Ingestor` 或 crawler 进入系统
2. 文档被切分、记录 metadata，并写入 notebook
3. `RAGPipeline` 管理 ingest / search / stats / delete / retention
4. notebook 和 tenant backend policy 参与后端选择
5. Dashboard / CLI / API 通过统一抽象进行操作

### 3.4 控制面链路

Dashboard 和 API 并不是孤立实现，而是围绕现有运行时对象提供：

- inventory
- detail
- preview
- history
- lightweight mutation

因此控制面和实际 runtime 行为是绑定的，不只是另写一层展示逻辑。

## 4. 仓库结构说明

阅读代码时，最重要的目录可以按职责理解：

- `zen_claw/agent/`
  - Agent loop、intent router、pool、tool selection、runtime orchestration
- `zen_claw/skills/`
  - built-in skills、manifest、技能相关说明与业务能力
- `zen_claw/knowledge/`
  - ingestion、notebook、retrieval、vector-store abstraction、RAG pipeline
- `zen_claw/dashboard/`
  - dashboard server 与 operator-facing API surface
- `zen_claw/channels/`
  - 各类 inbound / outbound 渠道接入
- `zen_claw/cron/`
  - 定时任务与 job orchestration
- `zen_claw/config/`
  - schema、配置解析、配置校验
- `zen_claw/telemetry/`、`zen_claw/observability/`
  - 统计、trace、usage、历史可视化相关能力
- `zen_claw/tunnel/`、`bridge/`、`browser/sidecar/`、`go/`
  - sidecar 与基础设施辅助组件
- `tests/`
  - 覆盖 runtime、CLI、dashboard、RAG、channels、crawler 的回归测试

## 5. 当前项目的开发者入口

如果你是第一次进入仓库，建议按这个顺序看：

1. `README.md`
2. `docs/repo_map.md`
3. `docs/verify_profile.md`
4. `zen_claw/cli/commands.py`
5. `zen_claw/dashboard/server.py`
6. `zen_claw/agent/intent_router.py`
7. `zen_claw/knowledge/pipeline.py`

其中：

- `cli/commands.py` 用来理解“这个项目对外暴露了什么操作入口”
- `dashboard/server.py` 用来理解“控制面现在已经做到了什么程度”
- `intent_router.py` 用来理解“直达意图、恢复机制、路由判定”
- `knowledge/pipeline.py` 用来理解“RAG 相关能力的统一抽象”

## 6. 当前边界与判断

从当前仓库状态来看，以下判断比较贴近现实：

- 这已经不是纯基础 demo，而是一个可持续扩展的 Agent 平台骨架
- 运行时、RAG、skills、dashboard、crawler 都有真实落地，而不是只有设计文档
- 但它仍然不是“全部模块完全收口”的成熟产品，很多 area 已经有第一层控制面和抽象，但更深的运营能力还在继续补齐
- README 或路线图中出现的历史目标，不应自动视为已全部完成，应该以当前代码和测试为准

尤其要注意：

- 当前项目的 Alpha 主线以 Python runtime / control plane / RAG / crawler / skills 为主，不应被可选 sidecar 的成熟度直接拉低。
- 当前仓库没有根级 `package.json`，`axios` 也不是直接 npm 依赖；Node 风险的重点在 `bridge/` 与 `browser/sidecar/` 的 lockfile、精确版本与受控安装流程。
- `bridge/` 现在应视为需要 lockfile 驱动的受控组件；`browser/sidecar/` 则是带锁文件的可选 Playwright sidecar。
- `zen_claw/dashboard/static/chat.html` 仍使用固定版本 CDN 资源，这属于当前保留的外部前端供应链边界。
- `bridge/README.md` 现在承担 WhatsApp bridge 的最小安装、构建、启动与 smoke 验收说明。

- 一部分能力依赖 optional extras
- 一部分能力依赖外部凭证、渠道配置或运行 sidecar
- 对外文档应优先描述稳定入口，不要把阶段性实验能力写成无条件可用能力

## 7. 当前可依赖的真源文档

在这个仓库里，下面几份文档更适合作为“真实当前状态”的入口：

- `docs/repo_map.md`
- `docs/verify_profile.md`
- `docs/DEPLOY.md`
- `docs/feature-summary_and_usage.md`
- `README.md`

如果你要判断“未来计划”而不是“现在已有”，再看：

- `docs/zen-claw功能补足开发计划.md`
- `tasks/todo.md`

## 8. 总结

`zen-claw` 当前最重要的特点，不是某一个单点功能，而是它已经把：

- Agent runtime
- 多 Agent 路由
- Skills
- Knowledge / RAG
- Dashboard / API
- Channels / crawler / cron

这些通常分散在多个项目里的能力，收束到了一个统一仓库中，并且很多部分已经有测试、CLI、控制面和审计路径支撑。

对于开发者来说，它最适合作为：

- 可继续演进的 Agent 平台基座
- 本地优先、可控执行的实验与业务原型框架
- 后续业务化 Agent 系统的技术底盘
