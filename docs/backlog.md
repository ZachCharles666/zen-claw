# zen-claw 技术 Backlog

> 记录待实施的架构演进项，按执行顺序排列。
> 评估基准文档：[`docs/design/MHA_b158_feasibility_assessment_20260406.md`](design/MHA_b158_feasibility_assessment_20260406.md)
> 最后更新：2026-04-10

---

## 执行顺序：C → A → B

方案三（三明治：b1.58 检索 → MHA 规划 → b1.58 顶层门控）已在可行性评估中确认为主线方向。

---

## Phase C — b1.58 顶层门控统一（优先级：最高）

**预估工期**：2 周  
**状态**：✅ 全部完成

### 已完成

| 工作项 | 位置 |
|--------|------|
| `ToolPolicyDecision.reason` 结构化输出（含 `code / tool / trigger / message` 字段） | `zen_claw/agent/tools/policy.py:26` |
| `PendingApproval.policy_reason: dict[str, str] \| None` 字段新增 | `zen_claw/agent/approval_gate.py:45` |
| `loop.py` 评估 policy 并将 `policy_reason` 传入 `request_approval()` | `zen_claw/agent/loop.py:2823–2863` |
| **C-1**：policy denied 时立即短路，写 `tool.policy_denied` audit 事件，不调用 `request_approval()` | `zen_claw/agent/loop.py:2828–2852` |
| C-1 回归测试：`test_policy_denied_does_not_create_pending_approval` | `tests/test_approval_gate.py` |

---

## Phase A — b1.58 底层三进制检索（优先级：中）

**预估工期**：3～6 周  
**状态**：核心实现完成（A-1/A-2/A-3/A-5 已落地，A-4 离线评测待补充）

### 目标

在现有 `TernaryRecallStrategy`（已实现三态分类 {−1, 0, +1}，位于 `zen_claw/agent/memory_ternary.py`）基础上，实现 `TriternaryRecallStrategy`，通过 `ContextBuilder` 的 `MemoryRecallStrategy` ABC 接入，进一步优化记忆召回的噪声过滤和 token 预算控制。

### 具体工作项

| # | 工作内容 | 状态 | 位置 |
|---|----------|------|------|
| A-1 | 实现 `TriternaryRecallStrategy`（`score()` 直接返回 {-1.0, 0.0, 1.0}） | ✅ 完成 | `zen_claw/agent/memory_ternary.py` |
| A-2 | 注册新模式 `memory_recall_mode = "trit"` | ✅ 完成 | `zen_claw/agent/context.py:67–88` |
| A-3 | 时序确认：recall 在 rolling summary 压缩前执行（已验证，无需额外改动） | ✅ 确认 | `zen_claw/agent/context.py` |
| A-4 | 离线对比实验：召回率、误召回率、token 节省量 | 待完成 | `pytest -m eval` 框架扩充 |
| A-5 | `"trit"` 加入 config schema Literal union | ✅ 完成 | `zen_claw/config/schema.py` |

**回归测试**：`TestTriternaryRecallStrategy` 11 个专项用例 → `tests/test_memory_ternary.py`

### 收益预期（已校准）

- **确定收益**：token 节省 + 噪声过滤（不确定候选不再占用上下文预算）
- **不确定收益**：延迟改善（向量后端几乎必然将 −1/0/1 强转为 Float32，实际检索速度不会显著提升）
- 延迟收益应在离线实验后单列观察，不作为主承诺

### 关键注意事项

1. **时序问题（实现前必须明确）**：向量数据库写入时间戳与 Agent 处理时间戳之间的对齐语义需要先定义，否则三态分类的边界条件在时序敏感场景下会产生不确定行为
2. **`ContextCompressor` 顺序**：三进制检索必须在 rolling summary 压缩之前介入，否则压缩已经截断的上下文会让三态分类失去意义

### 风险评估

**低** — `MemoryRecallStrategy` ABC 接口设计干净，实现只需完成 `score()` 方法；时序问题是唯一需要提前设计的非平凡点。

---

## Phase B — MHA 中层候选排序（优先级：低，定性为技术攻坚 Spike）

**预估工期**：6～8 周（乐观），实际可能更长  
**状态**：未开始（缺训练数据集和评测集，存在大规模测试回归风险）

### 目标

将 Gate 1 现有的顺序首次匹配（crystallized → declarative → native handler）改为三头并行评分 + 聚合排序，类似多头注意力从不同子空间聚合候选的方式，提升复杂意图下的路由精度。

### 具体工作项

| # | 工作内容 | 插入点 |
|---|----------|--------|
| B-1 | 建立离线路由评测数据集（Query → Candidate 正/负样本） | `tests/eval/` 目录扩充 |
| B-2 | 确认 `RouteCandidate` 数据结构的多头评分扩展方式 | `zen_claw/agent/orchestration.py` |
| B-3 | `IntentRouter` 新增多头并行评分 mixin 或替换 candidate ranking 逻辑 | `zen_claw/agent/intent_router.py` |
| B-4 | 增加"为何命中 / 为何未命中"证据字段，保障可审计性 | `RouteCandidate` |
| B-5 | 离线评测：若未显著优于"LLM 直接结构化路由"，保持为实验特性 | 评测集 |

### 关键风险

**中** — 主要风险不在实现复杂度，而在测试回归代价：

> 当前 191 个测试文件中大量围绕"可预测路由"写断言（例如：给定输入 X，断言路由到 intent Y）。MHA 引入的概率性排序会使部分断言不稳定，预计需要大规模改写或引入允许概率区间的断言模式。

### 前置条件（必须先满足才能开始）

- [ ] 训练/评测数据集建立完成
- [ ] 离线 routing accuracy eval 基准线确认（`pytest -m eval` 已有框架，需扩充样本）
- [ ] 对现有"可预测路由"测试制定迁移策略（改写 vs. 隔离 vs. 标记 `not mha`）
- [x] Phase C 完成（顶层门控统一后，层间语义才稳定，MHA 候选才有可靠的下游消费者）

### 执行约束

- 仅影响 Gate 1/Gate 2 的 candidate ranking，不允许越过现有 contract 直接生成执行计划
- 对外部副作用工具保留硬性 deny-by-default
- 若离线评测未显著优于 LLM 直接结构化路由，保持为实验特性，不进入默认链路

---

## 量化验收指标（来自可行性评估文档）

| # | 指标 | 门槛 |
|---|------|------|
| 1 | P95 端到端延迟 | 不劣化超过 15% |
| 2 | 工具误调用率（含错误 API） | 下降或持平 |
| 3 | 人工审批触发准确率 | 提升（减少无意义审批） |
| 4 | 同问题重试次数 | 不增加（防止层间打架） |
| 5 | 可解释性字段覆盖率 | 达到 95%+（每次拒绝/放行都有结构化原因） |
| 6 | 回归测试稳定性 | 关键路由测试通过率保持基线 |

---

## 参考

- 可行性评估原文：[`docs/design/MHA_b158_feasibility_assessment_20260406.md`](design/MHA_b158_feasibility_assessment_20260406.md)
- 已实现三态检索：`zen_claw/agent/memory_ternary.py`
- Phase C 现有测试基准：`tests/test_tool_policy_engine.py`、`tests/test_approval_gate.py`
- 离线路由评测框架：`pytest -m eval`
- Gate 1/2/3 架构说明：`docs/daily-assistant-architecture.md`
