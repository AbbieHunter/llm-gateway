# M4 开发计划（Dev Plan）· 语义缓存 / cost 路由策略 / CSV 导出 / 可观测面板 / PII·审核护栏

> 版本：v0.2（评审通过，决议 R1~R9）
> 日期：2026-07-24
> 评审纪要：`../meetings/M4_DEV_PLAN_REVIEW_MEETING.md`
> 范围：M4 里程碑（Phase 2 可选增强）—— 语义缓存、cost 路由策略、CSV 导出、可观测面板（Prometheus/Grafana）、PII / 审核护栏。本里程碑 5 条用户故事（US-M4-01~05）**全部为 Could（可选）**，按 US 约定"按资源与价值择机"推进。
> 关联：`../product/USER_STORIES.md`（US-M4-01~05，第 434–460 行 `## M4 · 可选增强（Phase 2）`）、`../product/PRD.md` §4、`../product/PRODUCT_DESIGN.md`、`ARCHITECTURE.md` v0.2 §4.5(路由)/§4.6(缓存)/§4.7(计量)/§4.8(可观测性)、`M3_DEV_PLAN.md`（前置：精确缓存 / 成本估算 / 用量报表三维 / 运行时状态 Redis）。
> 评审：已开模拟评审会（4 角色：PM/Dev/QA/用户），**通过，决议 R1~R9 回写本节升 v0.2，并回写 `ARCHITECTURE.md` §4.5/§4.6/§4.7/§4.8 漂移**。

---

## 1. 范围与目标

M0–M3 已交付「接入 + 账号 + 流式 + 额度 + 配额感知路由 + 精确缓存 + 三维报表 + Dashboard」的完整 MVP。M4 是在此之上的 **Phase 2 可选增强层**，目标不是"补齐必做功能"，而是给「已跑通的网关」叠加**降本、可运维、合规**三类增量能力：

1. **语义缓存（US-M4-01）**：相似 prompt 复用历史回答（embedding 相似度），在 M3 精确缓存之上再降一层成本。
2. **cost 路由策略（US-M4-02）**：Admin 选 `cost` 策略后，网关优先选最便宜的可达模型，直接省钱。
3. **CSV 导出（US-M4-03）**：用量报表可下载，运营/对账离线用。
4. **可观测面板（US-M4-04）**：暴露 Prometheus 指标 + 提供 Grafana 看板 JSON，运维可观测（延迟/错误率/各 provider 状态）。
5. **PII / 审核护栏（US-M4-05）**：进出流量 PII 脱敏与内容审核，合规优先、默认关闭、可选开关。

M4 覆盖用户故事 **US-M4-01 ~ US-M4-05（共 5 条，全部 Could）**。

**M4 完成目标（一句话）**：在不动 MVP 主路径的前提下，让网关"更省（语义缓存 + cost 路由）、更可运维（CSV + 面板）、更合规（护栏）"，且每项均可独立开关、互不影响。

> ⚠️ **M4 是可选里程碑**：5 条均为 Could。评审会已确认**执行顺序与全做**（见 §2.7 R9）。本计划默认 5 条全做，但任务相互独立，可裁剪。

---

## 2. 技术设计

> 关键前提：M3 已落地 `core/cache.py`（精确缓存）、`core/usage.py`（`estimate_cost` / `cost_is_estimated`）、`core/router.py`（failover/weighted + 配额过滤）、`core/health.py`（Redis 状态 `provider:{id}:status`）、`core/redis_client.py`。M4 在这些之上构建，不重复造轮子。

### 2.1 语义缓存（US-M4-01）

**Why**：精确缓存只命中"逐字节相同"的请求；真实流量里大量 prompt 只是措辞不同、语义相同（"帮我写封邮件" vs "写一封邮件"）。语义缓存把这一层也复用掉，进一步降本。

**落点**：扩展 `core/cache.py`（或新增 `core/semantic_cache.py`），在 M3 精确缓存之上叠加第二层。

**设计（R-arch-1~3）**：
- **分层**：`精确缓存（Tier1，M3）→ 语义缓存（Tier2，M4）`。每次请求先查精确缓存（命中即返）；未命中再查语义缓存（相似度 ≥ 阈值才命中）。精确缓存是"硬保证"，语义缓存是"软降本"。
- **Embedding 抽象**：新增可插拔 embedding 接口，默认走 OpenAI `text-embedding-3-small`（经 LiteLLM，复用 provider env 凭证）；可选本地模型。每条写入缓存时计算 embedding 并随响应一起存。
- **存储与检索**：embedding 向量 + 缓存响存在 Redis（复用 `redis_client`），按 `(alias/model)` 为作用域。个人/小团队规模下用**暴力余弦**扫描该作用域内缓存条目（几千条量级足够，无需外部向量库）；阈值默认 **0.92**（可配）。
- **仅非流式**：与 M3 精确缓存一致，流式不进语义缓存。
- **成本/延迟权衡**：embedding 调用只在**缓存 miss 路径**发生（命中零上游、零 embedding）；提供 `enable` 开关与按别名作用域，避免对延迟敏感场景强开。

**误命中红线（最严重风险）**：语义相似 ≠ 语义相同，阈值过低会把"不同问题"答成"相同答案"。缓解：高阈值默认 + 精确缓存硬层兜底 + 按别名隔离 + 可全局/按别名关闭。

### 2.2 cost 路由策略（US-M4-02）

**Why**：§4.5 已规划 `cost` 策略"推迟 Phase 2"。对同一能力，不同 provider 单价不同；让网关在可达集合里挑最便宜的，直接降低账单。

**落点**：`core/router.py`（新增 `cost` 策略排序）+ 新增 `model_prices` 单价表（SQLite 元数据，与 accounts/providers 同库）+ 路由策略选择器。

**设计（R-arch-4~5）**：
- **单价表**：`model_prices(provider, model, in_usd_per_1k, out_usd_per_1k, currency=USD, effective_from)`，种子来自内置 JSON（主流模型基准价），Admin 可覆盖编辑（API + 控制台）。MVP 仅 USD，不做多币种。
- **路由集成**：`model_routes.strategy` 在 `failover` / `weighted` 之外新增 `cost`。当策略 = `cost` 时，在 §4.5 步骤 2（过滤 `down`/`quota_exhausted`）之后，按**预估成本 = 单价 × 预估 token（prompt+completion，复用 M2/M3 `estimate_cost` 逻辑）**对候选排序，选最便宜且健康者。仍受配额感知与熔断约束。
- **依赖价格准确度**：选错最便宜源于价格表过期 → 提供"价格过时告警" + Admin 手动覆盖；排序用估算 token（非 OpenAI 为估算），可接受。

### 2.3 CSV 导出（US-M4-03）

**Why**：M2/M3 已做三维用量报表（控制台内看），但运营/对账常需离线表格。MVP 曾显式砍掉 CSV（§4.7），Phase 2 补回。

**落点**：扩展 `GET /api/usage`，新增 `format=csv`（默认 `json`）。

**设计（R-arch-6）**：
- 按当前 `group_by` + `range` 直接序列化为 CSV 文件响应（带 `Content-Disposition` 下载头）。
- **上限保护**：最大时间范围（默认 90 天）+ 最大行数（如 100k），超出返回 400 提示；避免一次性吐超大文件。不流式分块导出（MVP 足够）。
- **RBAC 不变**：user 仅见自身 VK（复用 M2/M3 越权 403 逻辑，导出与在线视图同源）。
- **Non-goals**：不做定时邮件投递、不做 Excel、不做趋势图（趋势图仍 Phase 2 后置）。
- 前端用量页加「导出 CSV」按钮，沿用当前筛选条件。

### 2.4 可观测面板（US-M4-04）

**Why**：§4.8 已设计 Prometheus 指标（请求数/错误率/p95 延迟/各 provider 健康配额/token 吞吐/配额误标记计数）。M4 把"设计"落为"可抓取端点 + 看板"。

**落点**：新增 `core/metrics.py`（Prometheus 打点）+ `GET /metrics` 端点 + `docs/grafana/dashboard.json` + `prometheus.yml` 抓取片段。

**设计（R-arch-7）**：
- **指标（对齐 §4.8）**：请求计数（by model/provider/status）、延迟直方图（p95）、错误率、`provider:{id}:status` 映射为健康/配额 gauge、token 吞吐、缓存命中率（精确+语义）、配额误标记计数。
- **打点位置**：统一接入层中间件（请求数/延迟/错误）、router（provider 选择/配额跳过）、cache（命中率）、usage（token/误标记）。
- **依赖**：新增 `prometheus-client`（纯 Python，3.13 兼容）；若踩 3.13 原生包坑，则**自写 `/metrics` 文本端点（零依赖）**兜底。
- **不内置 Grafana**：仅提供看板 JSON + 抓取配置，由用户自部署；不做日志分析、不做 Trace（OpenTelemetry/Langfuse 串联留待后续）。

### 2.5 PII / 审核护栏（US-M4-05）

**Why**：涉及个人信息处理需符合《个人信息保护法》/GDPR。进出流量可脱敏、可审核，合规优先、默认关闭。

**落点**：新增 `core/guardrails.py` + 接入层中间件（入站脱敏前置、出站审核后置）。

**设计（R-arch-8）**：
- **入站（请求）**：检测 PII（邮箱/手机号/身份证号等），按模式可选"仅检测"或"脱敏替换（[REDACTED]）"后再发上游。
- **出站（响应）**：对返回内容做 PII 掩码 + 可选内容审核（本地规则默认；可插拔 provider 审核 API）。
- **开关**：全局配置 `GUARDRAILS_ENABLED` + 按路由可选覆盖；**默认关闭**（个人/小团队按需开）。
- **合规红线**：**绝不将检测到的原始 PII 落库**；日志/用量归因只在脱敏后记录。仅做在途处理，不训练、不存储检测到的 PII。
- **Non-goals**：不做全量 DLP、不做模型微调、不存储 PII 用于其他目的。

### 2.6 依赖与前置

- **必须前置（已交付）**：US-M3-09（精确缓存 `core/cache.py`）、US-M2-06/US-M3-10（用量落库与三维报表 `/api/usage`）、US-M1-11（别名路由候选）、US-M3-02（配额过滤 `provider:{id}:status`）、US-M2-04/06（`estimate_cost` / `cost_is_estimated`）。
- **M4 新增模块**：`core/semantic_cache.py`（或扩 `cache.py`）、`core/metrics.py`、`core/guardrails.py`、`model_prices` 表、`cost` 策略（扩 `router.py`）、`/metrics` 端点、Grafana JSON。
- **复用**：统一 Redis 客户端、M3 mock echo（`MOCK_PROVIDER=1`，`{prefix}/echo`）、M2/M3 越权 403 逻辑。

### 2.7 关键设计决定（评审会 R1~R9 已确认）

| 编号 | 决定 | 说明 |
|------|------|------|
| R1 | 语义缓存分层 + seed 旁路 | 精确缓存（Tier1, M3）永远先查；语义缓存（Tier2, M4）仅 miss 后查；**`seed` 存在时跳过语义缓存**（确定性请求不软复用）。 |
| R2 | Embedding 可插拔 + 失败降级 | 默认 OpenAI `text-embedding-3-small`（经 LiteLLM）；测试用 fake；**embedding 调用失败→跳过语义层走上游，不阻断请求**；向量随响应同条目存 Redis。 |
| R3 | 相似度阈值 + 检索作用域 | 默认 0.92 可配，暴力余弦；**作用域精确到 `(provider/model)` 字符串、绝不跨模型复用**；仅非流式；QA 增"近义不同任务"对照用例（翻译 vs 总结）。 |
| R4 | 单价表存储 | 新增 `model_prices`（SQLite，同库），种子内置 JSON + Admin 可覆盖；MVP 仅用最新一行（`effective_from` 留列不维护历史）；仅 USD。 |
| R5 | cost 策略集成 | `model_routes.strategy` 增 `cost`；配额过滤后按 预估成本=单价×估算token 排序；**单价缺失候选排后有价候选之后仍可兜底**；同价 tie-break 按 failover 优先级；受 quota/熔断约束。 |
| R6 | CSV 导出上限 + 注入防护 | `format=csv` 扩 `/api/usage`；最大 90 天 / 100k 行；RBAC 复用；**CSV 公式注入防护**（首字符 `= + - @` 转义）；大文件走线程池；不做邮件/Excel/趋势图。 |
| R7 | 可观测性落地 | `prometheus-client`（踩 3.13 坑则自写零依赖 `/metrics`）；指标对齐 §4.8；廉价 counter/histogram；`/metrics` 开放但仅聚合、无 VK/PII/密钥；`METRICS_ENABLED` 可关；提供 Grafana JSON + 抓取配置。 |
| R8 | PII 护栏合规 + 流式限制 | 默认关闭、全局+按路由覆盖；入站可脱敏/仅检测，出站默认仅检测+可选审核、**掩码独立更严开关默认关**；**流式仅入站生效、出站 best-effort 写 Non-goals**；绝不落库原始 PII；本地正则+可插拔审核；多租户需 DPIA（当前标注）。 |
| R9 | 执行顺序 | 5 条全做可裁剪；顺序 **CSV(S)→可观测面板(M)→cost 路由(M)→语义缓存(L)→PII 护栏(L)**；合规硬 deadline 时 PII 提前至可观测之后；L 任务拆子任务摊薄风险。 |

---

## 3. 开发任务拆解（T-01 ~ T-05）

> T 恤尺码（S/M/L）为**建议值，待 Dev 在 M4 评审确认**。每个任务含可勾选子项、对应故事、依赖。5 条故事相互独立，可并行裁剪。

### T-01 · 语义缓存 — 【L】
- 对应：US-M4-01
- 依赖：US-M3-09（精确缓存 `core/cache.py`）、Redis
- [ ] Embedding 抽象（`core/semantic_cache.py` 或扩 `cache.py`）：默认 OpenAI embedding 经 LiteLLM，可插拔本地；测试用 fake embedding
- [ ] 缓存写入时计算并存储 embedding 向量（仅非流式）
- [ ] 精确缓存未命中后查语义缓存：暴力余弦扫 `(alias/model)` 作用域，相似度 ≥ 阈值命中返回
- [ ] 配置：`SEMANTIC_CACHE_ENABLE` / `SIMILARITY_THRESHOLD`（默认 0.92）/ 按别名作用域
- [ ] 单测：两条相似 prompt → 第二条零上游命中；不相似 → miss 调上游；不同别名不串；流式不进语义缓存
- 验收：语义缓存作为 Tier2 生效，误命中率可控；精确缓存硬层不受影响；开关可关

### T-02 · cost 路由策略 — 【M】
- 对应：US-M4-02
- 依赖：US-M1-11（router 候选）、US-M2-04/06（`estimate_cost`）、§4.5 路由
- [ ] 新增 `model_prices` 表（SQLite）+ 内置种子 JSON + Admin 覆盖 API
- [ ] `model_routes.strategy` 增 `cost`：`resolve` 在配额过滤后按 预估成本 排序选最便宜健康候选
- [ ] 控制台路由策略选择器加 `cost` 选项 + 单价编辑 UI
- [ ] 单测：两 provider 不同单价 → cost 策略选更便宜；`quota_exhausted` 仍被跳过；价格表缺失降级 failover
- 验收：cost 策略选出最便宜可达模型；价格表 Admin 可编辑；与配额感知/熔断不冲突

### T-03 · CSV 导出 — 【S】
- 对应：US-M4-03
- 依赖：US-M2-06 / US-M3-10（`/api/usage` 三维）
- [ ] `GET /api/usage` 增 `format=csv`：按当前 `group_by`+`range` 序列化 CSV 文件响应
- [ ] 上限保护（最大 90 天 / 100k 行），超限 400
- [ ] RBAC 越权 403 复用 M2/M3 逻辑
- [ ] 前端用量页「导出 CSV」按钮（沿用当前筛选）
- [ ] 单测：导出形状正确；上限拦截；user 仅见自身
- 验收：报表可下载；范围/维度正确；权限正确

### T-04 · 可观测面板 — 【M】
- 对应：US-M4-04
- 依赖：US-M3-02（Redis 状态）、US-M2-06（UsageLog）、结构化日志
- [ ] `core/metrics.py` 打点：请求计数 / 延迟直方图(p95) / 错误率 / provider 健康·配额 gauge / token 吞吐 / 缓存命中率 / 配额误标记计数
- [ ] `GET /metrics`（Prometheus 格式）；新增依赖 `prometheus-client`（踩坑则自写零依赖端点）
- [ ] 提供 `docs/grafana/dashboard.json` + `prometheus.yml` 抓取片段（不内置 Grafana）
- [ ] 单测：`/metrics` 暴露预期指标序列；打点随请求递增
- 验收：指标可被 Prometheus 抓取；Grafana JSON 可导入；不拖慢主路径

### T-05 · PII / 审核护栏 — 【L】
- 对应：US-M4-05
- 依赖：接入层中间件（§6.1）、合规要求
- [ ] `core/guardrails.py`：PII 检测（邮箱/手机/身份证等）+ 脱敏/替换；出站掩码 + 可选审核（本地规则默认，可插拔 API）
- [ ] 入站中间件（脱敏前置）+ 出站后处理（掩码/审核）；全局 `GUARDRAILS_ENABLED` + 按路由覆盖
- [ ] 合规：绝不落库原始 PII；日志/用量归因仅在脱敏后；默认关闭
- [ ] 单测：开启时 PII 被检测+脱敏、日志无原始 PII；关闭时透传；误报不改坏结构
- 验收：护栏可开关；进出流量合规处理；无 PII 落库

### 依赖关系
```
M3 精确缓存 / M2 用量 / M1 路由 (前置)
   ├─▶ T-01 语义缓存 (L, 独立模块, 依赖 M3 cache)
   ├─▶ T-02 cost 路由 (M, 依赖 router + 新 price 表)
   ├─▶ T-03 CSV 导出 (S, 依赖 /api/usage)
   ├─▶ T-04 可观测面板 (M, 独立模块)
   └─▶ T-05 PII 护栏 (L, 独立模块)
> 5 个任务相互独立，可并行；裁剪任一不影响其余。T-01/T-05 尺度最大，建议优先排期或拆子任务。
```

---

## 4. M4 完成定义（Definition of Done）

- [ ] 语义缓存：相似非流式 prompt 命中复用（零上游），精确缓存硬层不受影响；阈值/开关可配；误命中可控
- [ ] cost 路由策略：`model_routes.strategy=cost` 时选最便宜可达模型；单价表 Admin 可编辑；与配额感知/熔断不冲突
- [ ] CSV 导出：`/api/usage?format=csv` 按当前维度/范围导出，带上限保护；user 仅见自身
- [ ] 可观测面板：`/metrics` 暴露 §4.8 指标；提供 Grafana JSON + 抓取配置；可被 Prometheus 抓取
- [ ] PII / 审核护栏：进出流量可检测/脱敏/审核；默认关闭、可开关；无原始 PII 落库；日志脱敏后
- [ ] 每项能力独立开关，互不影响 MVP 主路径（M0–M3 行为不变）
- [ ] pytest 覆盖 5 条故事重点场景；`scripts/smoke.sh` 或 `scripts/smoke_m4.py` 扩展通过（复用 MOCK_PROVIDER）
- [ ] 架构 §4.5(cost 策略)/§4.6(语义缓存落地)/§4.7(CSV+单价表)/§4.8(/metrics 落地) 文档漂移回写对齐

### 非目标（M4 显式不做）
- 实时竞价 / 多币种结算（仅 USD 单价表）
- 模型微调 / 私有部署
- 全量 DLP / 训练或存储用户 PII
- Trace（OpenTelemetry / Langfuse 串联 fallback 链路，留待后续）
- 定时邮件投递报表 / Excel 导出 / 趋势图
- 跨 provider 金额硬限额（仅报表，沿用 PRD 决议）
- 细粒度 QPS / 并发限流（仍后置）

---

## 5. 风险与注意

- **语义缓存误命中（最高风险）**：相似 ≠ 相同，阈值低会答非所问。缓解：0.92 默认高阈值 + 精确缓存硬层兜底 + 按别名隔离 + 可全局/按别名关闭；QA 用相似/不相似对照验。
- **embedding 成本与延迟**：每次 miss 多一次 embedding 调用。缓解：仅 miss 路径、可配置、本地模型选项；测试用 fake embedding 免真 API。
- **价格表时效**：过期导致选错最便宜。缓解：内置基准价种子 + Admin 覆盖 + 过时告警；排序用估算 token（可接受）。
- **可观测性依赖边界**：`/metrics` 暴露但 Grafana 由用户自部署，不内置；`prometheus-client` 若踩 3.13 坑则自写零依赖端点。
- **PII 合规红线**：绝不落库检测到的原始 PII；默认关闭；本地规则误报可能改坏合法输出 → 提供"仅检测不替换"模式。涉及个人信息处理需评估（§4.8/PRD 合规要求）。
- **依赖纪律**：延续 M1/M3 铁律——新增依赖优先纯 Python，避开 3.13 原生包坑（litellm 已钉 `>=1.67,<2.0`）。
- **M4 可选性**：5 条均为 Could，评审会需确认最终执行顺序与是否全做（R-arch-9）；任一任务可裁剪不影响其余。
- **文档回写**：M4 评审通过后回写 `ARCHITECTURE.md` §4.5/§4.6/§4.7/§4.8 漂移，保持文档链一致。
