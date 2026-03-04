# zen-claw

[中文版 (Chinese Version)](#中文版)

zen-claw is a local-first AI agent execution framework offering controllable tool invocation, multi-channel integration, and traceable operational governance.

## Core Capabilities

### Agent & Execution
- Agent Loop + Tool Invocation + Reflection Iteration
- Runtime commands: `/model`, `/clear`, `/think`, `/usage`, `/verbose`
- Multi-agent routing & isolated workspaces: `~/.zen-claw/workspaces/<agent_id>/`

### Security & Governance
- Subagent sensitive tool guardrails
- Sidecar network proxy and execution proxy (optional)
- `config doctor` for configuration health checks and troubleshooting

### Context & Memory
- SQLite default memory retrieval and fallback strategies
- Token-aware context hysteresis (thresholds / fallback / cooldowns)
- Dashboard displaying context compression history

### Zero-Config Tunneling & Compliance (Phase 4 Features)
- **Cloudflare Tunnel Integration**: Native `zen-claw tunnel start` for secure gateway exposure.
- **Fortified Webhook Exposure Guards**: `X-Signature` validation, `X-Nonce` replay-prevention tracking, and automated IP/ASN blacklisting/circuit-breaking.
- **Centralized Registry AuthZ**: Ecosystem review repository enforcing the 4-Eyes principle alongside a one-click DMCA takedown mechanism.
- **TUF-style Trust & DR**: Hardcoded Root-of-Trust public keys enforcing RTO/RPO mechanisms with mandatory rollbacks for supply-chain integrity.

## Detailed Features & Usage

### 1. LLM & Conversation
Zen-Claw is a multi-channel AI agent. Beyond basic chat, it supports:
- **Runtime Model Switching**: Change models mid-conversation using `/model <name>` (e.g., `/model gpt-4o`).
- **Think Mode**: Toggle reasoning chains for models like DeepSeek Reasoner using `/think on/off`.
- **Context Management**: Use `/clear` to reset conversation context. Automatic token-aware compression prevents context overflow.

### 2. Skills & Subagents
- **Web Browsing**: Automatically uses a headless sidecar browser for web search and page reading.
- **Skill Registry**: Install verified skills via CLI: `zen-claw skill install <name_or_url>`.
- **Permission System**: High-risk actions (file write, shell exec) require explicit user approval.

### 3. Exposure Guards & Privacy
- **Cloudflare Tunnel**: Securely expose your local gateway to the public internet with `zen-claw tunnel start`.
- **Webhook Security**: POST requests to `/webhook/trigger/{agent_id}` require mandatory `X-Signature`, `X-Timestamp`, and `X-Nonce` headers to prevent replay attacks and unauthorized access.
- **Audit Logging**: All activity is recorded in `audit_logs/*.jsonl` with tenant isolation and PII masking.

### Multi-Channel Integration
- IM/Social Channels: Telegram, Discord, WhatsApp, Feishu, DingTalk, WeChat MP, WeCom
- Extended Channels: WebChat, Webhook Trigger, Slack, Signal, Matrix

## Channel Support Matrix

| Channel | Inbound | Outbound | Attachments | Notes |
| --- | --- | --- | --- | --- |
| WebChat | Yes | Yes | Yes | Dashboard `/chat/ws` via internal bus streaming |
| Webhook Trigger | Yes | N/A | N/A | Supports signatures, nonces, anti-replay, IP allowlist |
| Slack | Yes | Yes | Yes | Socket Mode + HTTP Events dual-mode |
| Signal | Yes | Yes | Yes | signald / signal-cli, supports attachment download mapping |
| Matrix | Yes | Yes | Yes | Supports auto register/login, E2EE paths |
| Telegram / Discord / WhatsApp / Feishu | Yes | Yes | Partial | Common basic production channels |

## Quick Start

### 1. Installation

```powershell
git clone https://github.com/ZachCharles666/zen-claw.git
cd zen-claw
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e .[dev]
```

### 2. Initialize Configuration

```powershell
zen-claw config wizard
zen-claw config doctor
```

Strict inspection mode:

```powershell
zen-claw config doctor --strict
```

### 3. Local Chat

```powershell
zen-claw agent -m "Hello"
```

### 4. Start Gateway & Dashboard

```powershell
zen-claw gateway --port 18790
zen-claw dashboard --host 127.0.0.1 --port 18791
```

## Common Commands

```powershell
zen-claw status -v
zen-claw config providers
zen-claw config troubleshoot
zen-claw skill install <path-or-url-or-market:name>
```

## Development Verification

Refer to `docs/verify_profile.md` for baselines. Standard sequence:

```powershell
E:\zen-claw-public\.venv\Scripts\python.exe -m ruff check .
E:\zen-claw-public\.venv\Scripts\python.exe -m pytest -q
```

## License

MIT

---

<span id="中文版"></span>
# 中文版

zen-claw 是一个本地优先的 AI 代理执行框架，提供可控的工具调用、多渠道接入、以及可追踪的运行治理能力。

## 核心能力

### Agent 与执行
- Agent Loop + 工具调用 + 反思迭代
- Runtime 命令：`/model`、`/clear`、`/think`、`/usage`、`/verbose`
- 多 Agent 路由与隔离工作区：`~/.zen-claw/workspaces/<agent_id>/`

### 安全与治理
- 子代理敏感工具护栏（Subagent Guardrail）
- Sidecar 网络代理与执行代理（可选）
- `config doctor` 配置体检与故障提示

### 上下文与记忆
- SQLite 默认记忆检索与降级策略
- Token 感知上下文压缩（阈值/回滞/冷却）
- Dashboard 展示压缩触发历史

### 零配置内网穿透与合规体系 (Phase 4 新特性)
- **Cloudflare Tunnel 集成**: 原生 `zen-claw tunnel start` 安全暴漏内部网关。
- **高防 Webhook 防线**: `X-Signature`, 防重放 `X-Nonce` 跟踪及自动化 IP/ASN 黑名单阻断。
- **中心化 RegistryAuthZ**: 面向四眼原则（4-Eyes）发布的生态审核库与一键 DMCA 下架机制。
- **TUFs 信托灾备**: 支持 RTO/RPO 机制的硬编码根公钥与强制回滚。

## 详细功能与指引

### 1. 核心大语言模型与对话能力
*   **模型热切 (Runtime Model Switching)**：发送 `/model gpt-4o` 直接切换模型，`/model default` 恢复默认。
*   **深度思考模式 (Think Mode)**：针对推理模型（如 DeepSeek Reasoner）开关思维链展示：`/think on` 或 `/think off`。
*   **会话重置与压缩**：`/clear` 清空上下文。系统内置 Token 感知压缩，自动处理长文对话。

### 2. 工具与技能扩展 (Skills & Subagents)
*   **Web 搜索与阅读**：自动触发基于 Playwright 的 Sidecar Browser 抓取网页内容。
*   **技能安装**：支持一键安装：`zen-claw skill install github_analyzer` 或其 Web URL。
*   **权限审批**：高危权限（文件写入、Shell 执行）默认需用户实时审批。

### 3. 安全防护与内网穿透
*   **Cloudflare Tunnel**：无需公网 IP 即可安全对外：`zen-claw tunnel start --port 18790`。
*   **Webhook 验签保护**：所有外部推送必须携带 `X-Signature`、`X-Timestamp` 及 `X-Nonce`。系统自动识别并惩罚恶意重放请求。
*   **操作审计**：所有敏感指令、多租户访问流水均记录于 `audit_logs/` 下。

### 多渠道接入
- IM/社交通道：Telegram、Discord、WhatsApp、Feishu、DingTalk、WeChat MP、WeCom
- 扩展通道：WebChat、Webhook Trigger、Slack、Signal、Matrix

## 渠道支持矩阵

| Channel | 入站 | 出站 | 附件 | 备注 |
| --- | --- | --- | --- | --- |
| WebChat | Yes | Yes | Yes | Dashboard `/chat/ws` 走 bus 流式链路 |
| Webhook Trigger | Yes | N/A | N/A | 支持签名、nonce、防重放、IP allowlist |
| Slack | Yes | Yes | Yes | Socket Mode + HTTP Events 双模式 |
| Signal | Yes | Yes | Yes | signald / signal-cli，支持附件下载映射 |
| Matrix | Yes | Yes | Yes | 支持 auto register/login、E2EE 路径 |
| Telegram / Discord / WhatsApp / Feishu | Yes | Yes | Partial | 生产常用基础通道 |

## 快速开始

### 1. 安装

```powershell
git clone https://github.com/ZachCharles666/zen-claw.git
cd zen-claw
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e .[dev]
```

### 2. 初始化配置

```powershell
zen-claw config wizard
zen-claw config doctor
```

严格检查模式：

```powershell
zen-claw config doctor --strict
```

### 3. 本地对话

```powershell
zen-claw agent -m "Hello"
```

### 4. 启动网关与仪表盘

```powershell
zen-claw gateway --port 18790
zen-claw dashboard --host 127.0.0.1 --port 18791
```

## 常用命令

```powershell
zen-claw status -v
zen-claw config providers
zen-claw config troubleshoot
zen-claw skill install <path-or-url-or-market:name>
```

## 开发验证

以 `docs/verify_profile.md` 为准，默认顺序：

```powershell
E:\zen-claw-public\.venv\Scripts\python.exe -m ruff check .
E:\zen-claw-public\.venv\Scripts\python.exe -m pytest -q
```

## 许可证

MIT
