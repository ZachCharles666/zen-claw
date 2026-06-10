# zen-claw · 小馋虾

> 本地优先、可审计、面向实际执行的 AI Agent 平台。

[English summary](#english-summary)

zen-claw 不是一个只负责把消息转发给大模型的聊天壳。当前代码库已经实现了一套 Alpha 阶段的 Agent 平台基线：多 Agent 路由、分层意图执行、工具与技能治理、知识库、多渠道接入、控制面 API，以及隔离高风险能力的安全 sidecar。

- 当前版本：`0.1.3.post5`
- 运行环境：Python `>=3.11`，主 CI 使用 Python `3.12` / Windows
- 许可证：Business Source License 1.1

## 项目判断

| 维度 | 当前代码状态 |
| --- | --- |
| 产品阶段 | Alpha。核心运行链路和治理能力已有较广测试覆盖，但各子系统成熟度不完全一致 |
| 核心优势 | 本地优先、执行路径可解释、高风险工具受策略与 sidecar 约束、控制面与审计能力完整度较高 |
| 主要形态 | Python 主运行时 + FastAPI 控制面 + 可选 Node/Go sidecar |
| 适合场景 | 私有 Agent 平台原型、个人/团队助手、多渠道 Bot、需要审批和审计的工具执行、RAG 与业务技能实验 |
| 当前边界 | 仍需自行配置模型、渠道和外部服务；生产部署需要按场景完成凭证、网络、存储和 sidecar 加固 |

## 已实现的核心架构

```text
CLI / Channels / Webhooks / OpenAI-compatible API
                         |
                  AgentRouter / AgentPool
                         |
          Intent routing -> execution handoff -> AgentLoop
                         |
       Skills / Tools / RAG / Sessions / Cron / Subagents
                         |
       Policy / Approval / Gateway / Audit / Sidecars
                         |
             Providers and external systems
```

### 1. Agent 运行时与路由

- `AgentLoop` 负责模型调用、工具迭代、规划、反思、上下文压缩和执行结果记录。
- 意图执行区分直达执行、受约束重规划、技能路径、自由规划、澄清和审批等待。
- `AgentRouter` 支持显式 Agent、会话绑定、关键词、渠道默认 Agent 和默认回退。
- `AgentPool` 为不同 Agent profile 解析独立工作区、模型、Prompt、技能和工具策略。
- 支持视觉模型、思考模型、回退模型、成本模型、稳定性模型和按意图/任务类型覆盖模型。

代码入口：

- `zen_claw/agent/loop.py`
- `zen_claw/agent/orchestration.py`
- `zen_claw/agent/router.py`
- `zen_claw/agent/pool.py`

### 2. 工具执行与安全治理

- 工具策略支持默认拒绝、profile 级 allow/deny、渠道约束、能力配额和生产加固校验。
- 高风险执行链路支持显式审批、HMAC 授权信封、审计事件和 sidecar 隔离。
- 网络搜索、抓取、浏览器、连接器和命令执行拥有独立配置与策略边界。
- Go sidecar 提供安全命令执行与网络代理；Node sidecar 提供浏览器自动化和 WhatsApp bridge。
- 配置默认启用 `production_hardening`，并拒绝不符合要求的 legacy compatibility 或非 HMAC sidecar 配置。

代码入口：

- `zen_claw/config/schema.py`
- `zen_claw/agent/approval_gate.py`
- `zen_claw/agent/tools/policy.py`
- `zen_claw/agent/tools/capability_policy.py`
- `zen_claw/gateway/`
- `go/sec-execd/`
- `go/net-proxy/`
- `browser/sidecar/`

### 3. Skills 与业务能力

Skills 系统已实现：

- 内置技能、工作区技能和多来源技能发现
- 启用、禁用、验证、测试、完整性校验、安装、卸载、导出和 SBOM
- Skills market 搜索、签名/哈希检查、版本降级防护和可信时间缓存
- profile 级技能预加载与运行时权限门

仓库中的内置技能包括内容生成、合规检查、Crawler、RAG 检索、数据库助手、浏览器自动化、知识管理、GDrive、天气等。具体可用性取决于对应依赖、凭证和运行环境。

```powershell
zen-claw skills list
zen-claw skills test content_gen
zen-claw skills verify-integrity
```

### 4. Knowledge / RAG

- `RAGPipeline` 提供 ingest、search、document lifecycle、retention 和 stats。
- 支持 notebook、tenant、稳定 document ID、元数据过滤和后端策略。
- 当前向量存储实现包括 Chroma 和内存存储。
- 提供 CLI、Dashboard 和 API 操作面。

RAG 的完整依赖不在基础安装中，需要安装 `rag` extra。

```powershell
python -m pip install -e ".[rag]"
zen-claw rag ingest .\docs --notebook project
zen-claw rag search "执行策略" --notebook project
zen-claw rag stats
```

### 5. 渠道、Webhook 与控制面

共享渠道注册表当前包含：

- WebChat
- Webhook Trigger
- WhatsApp
- Telegram
- Discord
- Slack
- Signal
- Matrix
- Feishu
- WeChat MP
- WeCom
- DingTalk

各渠道实现的认证方式、媒体能力、运行时控制方式和外部依赖不同。WebChat 支持进程内启动、停止、重启和 apply；其他渠道中的部分操作目前是配置或审计层操作。

Dashboard / FastAPI 控制面覆盖 Agent、Skills、渠道、运行摘要、模型路由、RAG、Crawler、Cron、审计和 OpenAI-compatible API。主要入口包括：

- `/api/status`
- `/api/v1/*`
- `/v1/models`
- `/v1/chat/completions`

### 6. 调度、Crawler 与外围能力

- Cron 服务支持定时任务及知识库/Crawler 任务。
- Crawler 支持来源目录、直接 HTTP 抽取、浏览器抽取、运行和调度。
- 支持 API keys、多租户、用户、凭证库、Agent 加密身份、移动节点任务和 TTS。
- 支持会话、记忆、上下文压缩、token 使用记录和执行可观测性。

## 快速开始

以下命令适用于 PowerShell 7：

```powershell
git clone https://github.com/ZachCharles666/zen-claw.git
Set-Location zen-claw

py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

zen-claw onboard
zen-claw config wizard
zen-claw config doctor --strict
zen-claw agent -m "你好，请介绍当前运行环境"
```

默认配置文件：

```text
~/.zen-claw/config.json
```

默认工作区：

```text
~/.zen-claw/workspace
```

## 常用命令

```powershell
# 查看状态和当前配置诊断
zen-claw status -v
zen-claw config providers
zen-claw config production-check

# Agent profiles
zen-claw agent list
zen-claw agent chat default
zen-claw agent test default

# Skills
zen-claw skills list
zen-claw skills test content_gen

# Knowledge / RAG
zen-claw rag stats
zen-claw knowledge list

# 渠道、任务与节点
zen-claw channels status
zen-claw cron list
zen-claw node list
```

查看完整命令树：

```powershell
zen-claw --help
zen-claw agent --help
zen-claw config --help
zen-claw skills --help
zen-claw rag --help
```

## 启动服务

### Dashboard / 控制面

```powershell
zen-claw dashboard --host 127.0.0.1 --port 18791
```

打开：<http://127.0.0.1:18791>

### Gateway / 渠道运行时

```powershell
zen-claw gateway --port 18790
```

Dashboard 与 Gateway 是独立 CLI 命令。需要同时运行时，请在两个 PowerShell 会话中分别启动。

## Docker

标准 Compose 配置会构建 Python 运行时、Go sidecar 和 WhatsApp bridge，并以 Dashboard 作为容器默认命令：

```powershell
Copy-Item .env.example .env
docker compose up -d zen-claw
docker compose logs -f zen-claw
```

可选 Chroma 服务：

```powershell
docker compose --profile rag up -d
```

当前 `docker-compose.yml` 的默认 `zen-claw` 命令只启动 Dashboard。若部署需要独立 Gateway 或其他 sidecar 生命周期，请在部署配置中显式编排。

## 可选依赖

| Extra | 用途 |
| --- | --- |
| `dev` | pytest、pytest-asyncio、Ruff |
| `eval` | OpenAI SDK 与评测工具 |
| `rag` | Chroma、embedding、PDF/DOCX 和文本抽取 |
| `tts` / `tts-all` | 语音合成 |
| `multitenant` | JWT 与密码哈希 |
| `calendar` | Google、Outlook、CalDAV 日历 |
| `notion` | Notion 接入 |
| `daily` | 日历与 Notion 的组合依赖 |

示例：

```powershell
python -m pip install -e ".[dev,rag,multitenant]"
```

## 模型与外部服务

配置 schema 为以下 provider 提供明确配置槽位：

- Anthropic
- OpenAI
- OpenRouter
- DeepSeek
- Groq
- Zhipu
- DashScope
- vLLM / OpenAI-compatible endpoint
- Gemini
- Moonshot
- AiHubMix

模型调用由 LiteLLM provider 适配层执行。渠道、语音、日历、Notion、GDrive、浏览器和搜索能力需要各自的凭证或外部运行时。

## 仓库结构

```text
zen_claw/
  agent/          Agent loop、路由、编排、记忆与工具
  api/            OpenAI-compatible API
  auth/           API key、身份、租户与用户
  channels/       共享渠道实现与注册表
  config/         Pydantic 配置 schema 与迁移
  dashboard/      FastAPI 控制面和 WebChat 页面
  gateway/        Gateway 控制平面
  knowledge/      RAG pipeline、notebook、store、retriever
  observability/  审计与 trace
  providers/      模型、转写与 TTS provider
  runtime/        sidecar 生命周期管理
  skills/         内置技能、manifest 和 registry
bridge/           WhatsApp Node bridge
browser/sidecar/  Playwright Node sidecar
go/               安全执行与网络代理 sidecar
tests/            行为、回归、策略、渠道与控制面测试
```

## 当前边界

- 项目处于 Alpha 阶段，不应默认视为无需加固即可直接承载生产流量。
- 基础安装不包含全部可选能力；RAG、多租户、日历、Notion 和语音等需要对应 extras。
- 多渠道“已注册”不等于零配置可用；每个渠道仍需要凭证、外部服务或 bridge。
- Dashboard 提供多种控制面 mutation API，但并非所有子系统都支持真正的进程内热加载。
- Go/Node sidecar 与 Python 主运行时有独立构建、配置和运行边界。
- 仓库中仍保留部分历史 `nano-claw` 包名或 module path；公开产品名与 Python CLI 均为 `zen-claw`。
- 当前没有 mypy 验证配置；静态检查基线是 Ruff。

## 开发与验证

仓库使用动态生成的验证配置。非平凡改动前先刷新：

```powershell
pwsh scripts/refresh_agent_context.ps1
```

当前基础验证顺序：

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -q
```

CI 还包含分片核心测试、运行时敏感测试、记忆召回测试、渠道矩阵和 nightly integration。

## 延伸阅读

- [当前架构设计与特色理念](docs/architecture-and-design-principles.md)
- [部署说明](docs/DEPLOY.md)
- [功能与使用说明](docs/feature-summary_and_usage.md)
- [自测手册](docs/self-test-and-playbook.md)
- [WhatsApp bridge](bridge/README.md)
- [安全说明](SECURITY.md)

## License

本项目采用 Business Source License 1.1。

许可证允许个人使用、学术研究、教育、非营利开源项目、组织内部开发/测试/评估/预发布，以及不构成许可证所定义“Commercial Production Use”的组织内部生产使用。向第三方提供托管服务，或将本项目嵌入面向第三方销售/授权的商业产品，需要商业许可。

Change Date 为 `2030-04-01`，届时转换为 Apache License 2.0。请以 [LICENSE](LICENSE) 全文为准。

---

## English Summary

zen-claw is a local-first, auditable AI agent platform currently at Alpha maturity. The implemented codebase combines:

- a multi-profile agent runtime with layered intent routing and explicit execution handoff;
- policy-controlled tools, approval flows, audit logs, and HMAC-protected sidecars;
- skills lifecycle management and a multi-source skills registry;
- notebook- and tenant-aware RAG;
- twelve registered messaging/webhook channels;
- a FastAPI dashboard/control plane and OpenAI-compatible endpoints;
- optional Go and Node sidecars for isolated execution, network proxying, browser automation, and WhatsApp.

The project is suitable for private agent-platform prototypes and controlled internal deployments. It still requires environment-specific provider credentials, channel setup, optional dependencies, and production hardening.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
zen-claw onboard
zen-claw config wizard
zen-claw agent -m "Hello"
```
