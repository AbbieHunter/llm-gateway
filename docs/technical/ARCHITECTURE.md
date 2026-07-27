# LLM Gateway 架构设计文档

> 版本：v0.2（校准版）
> 日期：2026-07-23
> 校准说明：基于 `../product/PRD.md` v0.1 评审会 + `../product/PRODUCT_DESIGN.md` v0.1 评审会的决议回写。
> 主要变更：① 补「前端（管理控制台）」模块（React 静态托管）；② 把"配额感知路由 / provider 错误码映射 / 额度耗尽标记 + 探活"并入路由与健康检查；③ 落定后端 RBAC 边界；④ 对齐评审决议（每日额度仅 token、路由 MVP=failover+weighted、凭证 env 引用+重启、砍 CSV/趋势图）。
> 场景：个人 / 小团队自用 · 技术栈：Python（后端）+ React（前端）

---

## 1. 背景与目标

### 1.1 为什么需要网关
直连各家大模型 API 会重复遇到：接口碎片化、密钥与成本黑盒、单点脆弱（某家额度耗尽/超时）、可观测性缺失。网关收敛成一个 **OpenAI 兼容统一入口**，应用侧几乎零改动即可在多家模型间切换，并把鉴权、限流、路由、缓存、计量、可观测统一在一处。

### 1.2 范围（Scope）
本期三项核心能力 + 一个管理控制台：
1. **统一接入**：OpenAI 兼容协议、多 provider 适配、虚拟 key 鉴权。
2. **成本与用量管控**：token 计费、按 key 每日额度、用量报表。
3. **高可用与故障转移（含配额感知路由）**：健康检查、超时重试、fallback 链、熔断、**token 额度耗尽自动切换**。
4. **管理控制台（前端）**：admin/user 登录、Key/Provider/路由/用量/账号的可视化管理。

安全合规（PII 脱敏、内容审核、prompt 注入防护）列为 **Phase 2 可选**。

### 1.3 非目标
- 不训练/托管模型，只做调度与治理。
- 不做多租户 SaaS（单租户，账号仅作角色区分）。
- 不做细粒度 QPS / 并发限流（MVP 后置）。
- 不做终端用户聊天界面（前端形态=管理控制台）。
- 不做模型微调/私有部署。
- 不做 PII/审核护栏（Phase 2）。

---

## 2. 总体架构

网关对外有**两个接入面**，均由同一个 FastAPI 进程提供：
- **网关 API（`/v1/*`）**：供开发者应用调用，OpenAI 兼容，用**虚拟 key** 鉴权。
- **控制台 API（`/api/*`）+ 静态 SPA**：供管理员/用户使用，用**账号会话**鉴权（RBAC）。

```
┌──────────────────────────────────────────────────────────────────┐
│                           接入方                                    │
│  ① 开发者应用/SDK（OpenAI 兼容，base_url 指向网关）                  │
│  ② 浏览器（管理控制台 SPA，React 静态资源由 FastAPI 托管）           │
└───────────┬───────────────────────────────────┬──────────────────┘
            │ HTTPS /v1/* (VK 鉴权)             │ HTTPS /api/* + / (会话鉴权)
            ▼                                    ▼
┌──────────────────────────────────────────────────────────────────┐
│                      LLM Gateway (FastAPI 单进程)                  │
│                                                                    │
│  ┌─────────────── 网关 API 中间件链 ───────────────┐              │
│  │ VK Auth → RateLimit → Router → Cache → Adapter   │              │
│  │   → Upstream → Metering → Observability          │              │
│  └───────────────────────┬────────────────────────┘              │
│  ┌─────────────── 控制台 API（RBAC）───────────────┐              │
│  │ Session Auth → 角色/所有权校验 → 业务 handler     │              │
│  │   （账号/Key/Provider/路由/用量/报表 CRUD）       │              │
│  └───────────────────────┬────────────────────────┘              │
│  ┌──────────────── 治理核心 (core/) ────────────────┐             │
│  │ Router(含配额感知+fallback+熔断) · errors(错误码映射)│           │
│  │ probe(额度耗尽探活) · adapters(LiteLLM) · cache    │            │
│  └───────────────────────┬────────────────────────┘              │
│  ┌──────── SPA 静态托管 (StaticFiles) ────────┐                  │
│  │ 生产构建产物 (frontend/dist) 由 FastAPI 直接 serve │              │
│  └────────────────────────────────────────────┘                  │
└───────────┬──────────────────────────────────────────────────────┘
            │ httpx async streaming
            ▼
   ┌──────────────────────────────┐
   │  Upstream Providers          │
   │  OpenAI / Anthropic / Google │
   │  / DeepSeek / 通义 / 文心    │
   └──────────────────────────────┘

支撑组件：Redis（缓存/限流/provider 状态）、SQLite/Postgres（账号/keys/路由/用量）、
         结构化日志 + Prometheus metrics
```

> 部署形态：**单仓库、单进程**。前端 `npm run build` 产物由 FastAPI 的 `StaticFiles` 挂载，不引入独立前端服务/反向代理。

---

## 3. 技术选型

| 关注点 | 选型 | 理由 |
|--------|------|------|
| Web 框架 | **FastAPI** | async、OpenAPI、依赖注入适合做两套鉴权中间件 |
| HTTP 客户端 | **httpx** | 异步 + 原生 streaming |
| 数据校验 | **Pydantic v2** | 与 OpenAI 规范对齐 |
| Provider 适配底座 | **LiteLLM (Python SDK)** | 300+ 模型格式归一化与路由，import 即用 |
| **前端框架** | **React + Vite + Tailwind** | 构建快、生态好；产物静态托管，零额外服务 |
| 缓存 / 限流计数 / Provider 状态 | **Redis** | 精确缓存、滑动窗口限流、provider 健康/耗尽标记 |
| 元数据存储 | **SQLite**（起步）→ Postgres | 账号/keys/路由/用量；SQLAlchemy 抽象 |
| 配置 | **环境变量（凭证）+ DB（配置）** | provider 凭据走 env（不落库）；provider/路由配置存 DB（控制台管理） |
| 可观测性 | **结构化日志 + Prometheus metrics** | 小团队先用日志+metrics，Grafana 按需 |
| 容器化 | **Docker / docker-compose** | 一键起网关 + Redis |

> 关键判断：用 **LiteLLM SDK 做适配与路由底座**，自研 FastAPI 写"接入层 + 治理层 + 控制台"。单进程同时 serve `/v1/*`、控制台 `/api/*` 与 SPA 静态资源。

---

## 4. 模块详细设计

### 4.1 统一接入层（网关 API `/v1/*`）
- `POST /v1/chat/completions`：核心，支持 `stream=true/false`。
- `GET /v1/models`：返回可见模型列表（含路由别名）。
- `POST /v1/embeddings`：可选，支撑语义缓存。
请求/响应用 Pydantic 对齐 OpenAI 规范；function calling / tool calls 原样透传（适配层映射）。

### 4.2 Provider 适配层（Adapters）
基于 LiteLLM SDK：`litellm.acompletion(...)` 内部处理各家流式分片、tool call、错误归一化。网关在其上做：
- **模型别名映射**：如 `fast-chat` → `openai/gpt-4o-mini` 或 `deepseek/deepseek-chat`。
- **凭证注入**：从环境变量解析真实 key，注入调用（**密钥永不落库**）。
- **错误归一化增强**（见 §4.5 `errors.py`）：把 provider 原始错误映射为本网关统一错误类别，供限流/熔断/配额标记判断。

> ⚠️ 新增/更换 provider 需**配置对应环境变量并重启网关**（密钥不落库的设计代价）。文档与控制台须明确提示，Phase 2 再评估"加密入库 + 热加载"。

### 4.3 鉴权、账号与 RBAC
**两套鉴权，职责分明：**

**(a) 网关 API — 虚拟 Key（VK）鉴权**
- VK：随机 32 字节 hex，入库只存 **SHA-256 哈希**，明文仅创建时返回一次。
- 中间件校验 VK 有效性/禁用/过期，命中后把 `account_id` / `quota_policy` 注入请求上下文（用量归因到账号）。
- VK 绑定所属 `account_id`（owner）。

**(b) 控制台 API — 账号会话鉴权 + RBAC**
- 账号表：`username / password_hash / role(admin|user) / status`。登录发会话（JWT 或签名 cookie）。
- **角色权限矩阵**（后端强制，前端隐藏菜单仅 UX）：
  | 资源/操作 | admin | user |
  |-----------|-------|------|
  | 账号管理（CRUD/改角色） | ✓ | ✗ |
  | Provider 配置 | ✓ | ✗ |
  | 路由策略 | ✓ | ✗ |
  | 虚拟 Key（全局/他人） | ✓ | 仅自身 |
  | 用量报表（全局） | ✓ | 仅自身 |
  | Dashboard | 全局视图 | 仅自身聚合 |
- **所有权过滤**：user 的所有查询按 `owner_account_id` 强制过滤，**后端为唯一真相源**——即使前端隐藏了菜单，直接调 API 也拿不到他人数据（QA 必须覆盖越权用例）。

### 4.4 限流与配额（Rate Limit）
- **MVP：按 VK 设置每日 token 额度（硬限额）**，本地时区自然日重置，超限返回 `429` + `Retry-After`。
  - 计数器存 Redis（带 TTL 到次日 00:00 本地时区）。
  - **每日额度单位 = token 计数**（跨 provider 通用、可归因）；**成本/金额仅作报表展示，不做金额硬限额**（评审决议：跨 provider 金额限额算不准）。
- 细粒度 QPS / 并发限流为**后置项**，本期不做。
- 数据模型 `virtual_keys.daily_token_quota INT NULL`（null=不限制）；**注意：早期草案曾用 `quota_policy` JSON（`{"daily_tokens":...}`），M2 实现已改为 `daily_token_quota` 直接列，本文档以列为准**。

### 4.5 路由、故障转移与配额感知（核心）
**路由决策**（缓存未命中后）：
1. 解析目标别名 → 候选 provider 有序列表。
2. **过滤不可用**：跳过 `down`（网络/5xx 降级）与 `quota_exhausted`（额度耗尽标记）的候选（完整模型串，见 Plan-B）。
3. 按策略排序（**MVP = `failover` + `weighted`**；`cost` 策略 **M4 已落地**：配额过滤后按 预估成本 = 单价×估算token 排序选最便宜可达候选，单价缺失候选排后有价候选之后仍可兜底，同价 tie-break 按 failover 优先级）：
   - `failover`：严格按列表顺序，主备切换（配额感知路由的默认）。
   - `weighted`：权重轮询。
4. 选主 provider 调用；可重试错误按 fallback 链尝试下一个。

**错误分类与映射（`errors.py`）——本网关最关键的一块**
> 评审核心结论：**「额度耗尽」与「限流 429」同为 429，必须靠错误体里的 code 区分，不能只看 HTTP 状态**，否则会漏标或误标。

| 内部类别 | 触发条件（按 provider 错误 code，非仅 HTTP 状态） | 处理 |
|----------|--------------------------------------------------|------|
| `QUOTA_EXHAUSTED` | 余额/quota 耗尽。OpenAI `insufficient_quota`；DeepSeek `402 insufficient_balance`；通义 `QuotaExceeded`/`AccountBalanceInsufficient` | **标记该候选模型（完整模型串）为 `quota_exhausted`**、跳过、fallback；**不重试**（重试无意义） |
| `RATE_LIMITED` | 通用 429（非上述 quota code） | 重试 + 退避（被限流可能恢复）；**不标 quota_exhausted** |
| `AUTH_ERROR` | 401/403 错误 key | **不重试、不标记**（配置问题） |
| `UPSTREAM_5XX` / `TIMEOUT` | 超时/5xx | 重试 + 退避；连续失败率高则标 `degraded` + 熔断 |

- MVP 优先做准 **OpenAI / DeepSeek / 通义** 三家映射，长尾后续补。
- **误标记红线**：仅当错误 code 明确指示 quota 才标记；绝不因通用 429/5xx 推断 quota。健康指标目标：**配额误标记率 ≈ 0**。
- **候选运行时状态存 Redis（不落关系库）**：键 `provider:{id}:status`（id = 完整候选模型串，如 `openai/qwen-plus-2025-12-01`），值 ∈ `healthy | degraded | down | quota_exhausted`；路由与健康检查读此键（M3 R8 回写，与 §4.5 状态机一致）。**Plan-B（2026-07-27）：状态键从 provider 前缀改为完整候选模型串**——同一 `openai/` 前缀下的多个模型（如多个免费模型各 1M 额度）互不干扰，某模型额度耗尽只跳过该模型，其余继续服务（熔断/标记均按候选粒度）。

**额度耗尽标记与探活（状态机）**
```
healthy ──(观测到 QUOTA_EXHAUSTED)──▶ quota_exhausted
   ▲                                      │
   │       手动重置(管理员) / 探活成功      │
   └──────────────────────────────────────┘
quota_exhausted ──(后台探活: 周期性 tiny completion)──▶ 成功则回 healthy
```
- **探活（`probe.py`）**：后台任务对 `quota_exhausted` 的候选模型周期性（如每 10 分钟，带冷却）发一个极小 completion（如 `max_tokens=1` 的 "hi"），成功则清除标记。探活自身网络错误**不**翻转为 `down`（配额与网络是两个维度）。
- **手动重置**：管理员在控制台 Dashboard「近期异常」列表或 Provider 页一键重置（评审决议保留此入口）。

**重试与熔断**
- 重试仅对**幂等可重试**错误（超时、5xx、限流 429），指数退避 + 抖动，上限 2~3 次；**禁止对 4xx（除 429）重试**。
- 熔断：连续失败率超阈值（如 50%/30s）短暂熔断，防重试风暴。

### 4.6 缓存（Cache）
- **精确缓存（Tier1，M3）**：`(model, 归一化 request_hash, stream=false)` KV 缓存，TTL 1h 可配。命中零上游成本，是"硬保证"。归一化键含 `model + messages + temperature + top_p + seed`；`stream` 不入键。
- **语义缓存（Tier2，M4）**：在精确缓存 miss 后，对 `(provider/model)` 作用域内缓存条目做暴力余弦相似度检索，≥ 阈值（默认 0.92 可配）命中复用，进一步降本。
  - **边界（M4 R1~R3）**：精确缓存永远先查；`seed` 存在时跳过语义缓存（确定性不软复用）；作用域精确到具体模型字符串、**绝不跨模型复用**；仅非流式；embedding 仅在 miss 路径计算（默认 `bge-small-zh-v1.5`，经本地 OpenAI 兼容 embedding 服务，由 `SEMANTIC_EMBEDDING_API_BASE` 指定；可插拔、测试用 fake；embedding 失败→跳过语义层走上游，不阻断）。
  - **误命中红线**：相似 ≠ 相同；靠 0.92 高阈值 + 精确缓存硬层兜底 + 按模型隔离 + 全局/按别名可关缓解；QA 含"近义不同任务"对照用例（如翻译 vs 总结）。

### 4.7 成本与用量计量（Metering）
- 每次调用记录：`vk_id, account_id, model, provider, prompt_tokens, completion_tokens, cost_usd, latency_ms, status, timestamp`。
- **token 计数**：优先上游返回；非 OpenAI 用 LiteLLM `token_counter` 近似，界面与报表**明确标注「估算」**。
- **成本**：`model → 单价` 配置表换算；**非 OpenAI 金额仅估算、仅供参考，关键账单以 provider 为准**。成本不做硬性限额（见 §4.4）。
- 报表：按 VK / 模型 / 时间聚合，经控制台 `/api/usage` 展示（M3 三维：`group_by=key|model|time` + `range=day|week|month`）。**M4 新增 `GET /api/usage?format=csv` 导出（最大 90 天 / 100k 行、CSV 公式注入防护、RBAC 越权 403 复用）**；趋势图仍后置。保留 Dashboard「近期异常」列表。
- **单价表 `model_prices`（M4）**：`(provider, model, in_usd_per_1k, out_usd_per_1k, currency=USD, effective_from)`，种子内置基准价 + Admin 可覆盖；MVP 仅用最新一行（`effective_from` 留列不维护历史），仅供 `cost` 策略与成本展示参考；仅 USD。

### 4.8 可观测性（Observability）
- 结构化日志：VK、路由路径、provider、耗时、token、状态。
- **`/metrics` 端点（M4）**：对齐以下 Prometheus 指标——请求计数（by model/provider/status）、延迟直方图（p95）、错误率、`provider:{id}:status` 健康/配额 gauge、token 吞吐、缓存命中率（精确+语义）、配额误标记计数。实现用 `prometheus-client`（踩 3.13 坑则自写零依赖 `/metrics` 文本端点兜底）；打点用廉价 counter/histogram，热路径零重计算；`/metrics` 默认开放抓取但**仅含聚合计数，绝不暴露 VK/PII/密钥**；提供 `METRICS_ENABLED` 开关可关。附 `docs/grafana/dashboard.json` + `prometheus.yml` 抓取片段，**不内置 Grafana**。
- Trace（可选）：OpenTelemetry / Langfuse 串联 fallback 链路（M4 未做）。

### 4.9 前端（管理控制台）
- **技术**：React + Vite + Tailwind；构建产物由 FastAPI `StaticFiles` 托管，**单仓库单进程**。
- **信息架构**（详见 `../product/PRODUCT_DESIGN.md`）：登录 → Dashboard / 虚拟 Key / Provider / 路由策略 / 用量报表 / 账号管理（admin 专属）。user 登录后左侧导航隐藏管理类菜单（UX 层）。
- **与后端契约**：
  - 所有数据经 `/api/*`，请求带会话凭证。
  - 敏感信息：VK 明文仅创建成功页一次展示 + 复制；Provider 凭证仅填**环境变量名**。
  - 破坏性操作（删 Key、禁用 provider、改角色）二次确认。
- **状态处理**：provider 全挂 → 网关回 `502` + 错误明细（让使用者看清是网关侧非自身代码）；额度耗尽 → 徽标 + 重置；限流 → `429` + 进度条预警。
- 桌面优先，不做移动端。

---

## 5. 数据模型（SQLite / Postgres）

```sql
-- 账号（RBAC）
CREATE TABLE accounts (
    id            TEXT PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,          -- 口令哈希（如 bcrypt/argon2）
    role          TEXT NOT NULL DEFAULT 'user',  -- admin | user
    status        TEXT DEFAULT 'active',  -- active | disabled
    created_at    TIMESTAMP
);

-- 虚拟 key
CREATE TABLE virtual_keys (
    id              TEXT PRIMARY KEY,
    key_hash        TEXT UNIQUE NOT NULL,  -- SHA-256 of VK
    name            TEXT,
    owner_account_id TEXT NOT NULL REFERENCES accounts(id),
    status          TEXT DEFAULT 'active', -- active | disabled
    daily_token_quota INT NULL,            -- M2 实现：每日 token 硬限额（null=不限制）；early 草案的 expires_at / quota_policy JSON 已废弃
    created_at      TIMESTAMP
);

-- provider 配置（凭据仅存 env 引用，真实值不落库）
CREATE TABLE providers (
    id            TEXT PRIMARY KEY,        -- openai / deepseek / qwen
    display_name  TEXT,
    auth_ref      TEXT,                    -- provider 前缀, e.g. openai/deepseek/qwen（与 LiteLLM env 约定一致，真实 env 为 OPENAI_API_KEY 等，自动读取；真 key 不落库）
    priority      INT,
    weight        REAL,
    enabled       BOOLEAN DEFAULT TRUE
);
-- provider 运行时状态(healthy/degraded/down/quota_exhausted) 存 Redis，不落关系库

-- 模型别名 -> provider 映射（控制台管理）
CREATE TABLE model_routes (
    alias         TEXT PRIMARY KEY,        -- fast-chat
    providers     TEXT,                    -- JSON 有序 list of LiteLLM 模型串, e.g. ["openai/gpt-4o-mini","deepseek/deepseek-chat"]（第一项为首选）；provider 前缀 = split('/')[0]，现切查 enabled/健康状态
    strategy      TEXT DEFAULT 'failover'  -- MVP: failover | weighted；cost 为 Phase 2
);

-- 用量明细
CREATE TABLE usage_logs (
    id              TEXT PRIMARY KEY,
    vk_id           TEXT,
    account_id      TEXT,
    model           TEXT,
    provider        TEXT,
    prompt_tokens   INT,
    completion_tokens INT,
    cost_usd        REAL,                  -- 非 OpenAI 为估算
    cost_is_estimated BOOLEAN DEFAULT FALSE,
    latency_ms      INT,
    status          TEXT,
    created_at      TIMESTAMP
);
CREATE INDEX idx_usage_account_time ON usage_logs(account_id, created_at);
CREATE INDEX idx_usage_vk_time ON usage_logs(vk_id, created_at);
```

> provider / 路由配置以 **DB 为准（控制台管理）**；`config/gateway.yaml` 仅作首次启动的可选 seed。高频状态（健康/耗尽/限流计数）一律放 Redis。

---

## 6. 关键流程

### 6.1 网关请求生命周期（`/v1/*`）
```
Client(应用)
 → [VK Auth] 哈希比对，注入 account_id/quota_policy
 → [RateLimit] 每日 token 额度检查，超阈 429
 → [Router] 解析别名 → 过滤 down/quota_exhausted → 按策略排序候选
 → [Cache] 精确缓存命中？→ 直接返回
 → [Adapter] 选主 provider，注入 env 凭证，调 LiteLLM
     失败(可重试)？→ fallback 链下一 provider（退避重试）
     失败且为 QUOTA_EXHAUSTED？→ 标该候选 quota_exhausted + 切下一候选（其余同前缀候选不受影响，Plan-B）
 → [Metering] 记录 token/成本/延迟/状态（非OpenAI标估算）
 → [Observability] 日志 + metrics
 → 响应返回（流式逐块转发）
```

### 6.2 流式（SSE）处理
- 经 **LiteLLM 直连**：`litellm.acompletion(model=..., stream=True)` 返回异步迭代器，`StreamingResponse` 逐块重帧 `data: {chunk.model_dump_json()}\n\n`，结尾 `data: [DONE]\n\n`（与 M1 「LiteLLM 直连」决策一致，不单独引 `httpx.AsyncClient.stream()`）。
- **错误传播**：上游流中途报错，向客户端补发 OpenAI 错误 event（**禁止静默断流**）。
- **客户端断开**：捕获 `CancelledError` + `request.is_disconnected()`，主动 `stream.aclose()` cancel 上游，避免悬挂与费用浪费；同时累加当日配额计数 partial tokens。
- **计量**：流式结束/异常/断开后按实际 token 结算（末块 `usage`；断开取已知 partial）。

### 6.3 Fallback 与配额标记
```
attempt on primary provider
 ├─ success → 返回
 ├─ QUOTA_EXHAUSTED → 标该候选 quota_exhausted，跳到 fallback 链下一候选（不重试）
 └─ retryable(timeout/5xx/限流429)
       ├─ 重试≤N仍失败 → 标 degraded，切下一 provider
       └─ 全部失败 → 返回统一 502 + 错误明细（含各 provider 失败原因）
```
> 502 明细必须清晰，让开发者分清"是网关侧 provider 全不可用"而非自身代码问题（评审用户红线）。

### 6.4 控制台登录与鉴权（`/api/*`）
```
浏览器 → 登录(username+pwd) → 会话凭证
 → 每次 /api 请求经 [Session Auth] 校验会话 + 角色
 → handler 内按 owner_account_id 过滤（user 仅自身）
 → 返回数据（前端按角色隐藏菜单仅 UX）
```

### 6.5 额度耗尽探活（后台）
```
定时(每~10min, 带冷却) 扫描 Redis 中 quota_exhausted 的候选模型
 → 发 tiny completion（max_tokens=1）用其 env 凭证
 → 成功 → 清除标记回 healthy
 → 仍 quota / 网络错 → 保持（网络错不翻转为 down）
```

---

## 7. 配置与部署

**目录结构（单仓库）**
```
llm-gateway/
├── app/                          # FastAPI 后端
│   ├── main.py                   # 入口：挂载 /v1、/api、SPA 静态
│   ├── routers/
│   │   ├── openai.py             # /v1/* 网关 API（VK 鉴权）
│   │   └── console.py            # /api/* 控制台 API（会话鉴权+RBAC）
│   ├── middleware/
│   │   ├── vk_auth.py            # 虚拟 key 鉴权
│   │   └── session_auth.py       # 账号会话鉴权 + 角色校验
│   ├── core/
│   │   ├── router.py             # 路由 + 健康 + 配额标记 + 熔断
│   │   ├── errors.py             # provider 错误码映射（关键）
│   │   ├── probe.py              # 额度耗尽探活
│   │   ├── adapters.py           # LiteLLM 封装
│   │   ├── cache.py
│   │   ├── metering.py
│   │   └── observability.py
│   ├── db/                       # SQLAlchemy 模型 + 迁移
│   └── config.py                 # env 加载
├── frontend/                     # React + Vite + Tailwind SPA
│   ├── src/
│   │   ├── pages/                # Dashboard/Keys/Providers/Routes/Reports/Accounts
│   │   ├── components/
│   │   ├── api/                  # 调 /api/*
│   │   └── auth/                 # 登录 + 会话
│   ├── vite.config.ts            # build → app/static/dist
│   └── package.json
├── config/gateway.yaml           # 可选首次 seed
├── migrations/
├── docker-compose.yml            # 网关 + redis
└── tests/
```

**环境变量（凭证，不落库）**
```
OPENAI_API_KEY=sk-...
DEEPSEEK_API_KEY=sk-...
QWEN_API_KEY=sk-...
# 新增 provider 需在此添加并重启网关
```

**部署**：`docker-compose up` 起网关 + Redis；生产前端 `npm run build` 产物由 FastAPI `StaticFiles` serve。网关前可放 Caddy/Nginx 做 TLS。小团队单机即可；横向扩展时限流/缓存/状态需共享 Redis。

---

## 8. 实施里程碑（校准）

| 阶段 | 内容 | 验收 |
|------|------|------|
| **M0** | FastAPI 骨架 + `/v1/chat/completions`（非流式）+ 单 provider（经 LiteLLM）直连 + SPA 空壳托管 | SDK 改 base_url 能对话；页面可加载 |
| **M1** | 账号体系 + RBAC + 虚拟 key 鉴权 + 多 provider 适配 + 路由（failover+weighted + 健康过滤）+ 控制台基础页（登录/Key/Provider/路由） | VK 校验生效；多 provider 切换；登录与角色鉴权生效，user 不能越权 |
| **M2** | 流式 SSE + 每日 token 额度限流 + 用量日志（SQLite） | 流式可用；超额度 429；用量可查且按账号归因 |
| **M3** | fallback/重试/熔断 + **配额感知路由（错误码映射 + quota_exhausted 标记 + 探活）** + 精确缓存 + 成本报表（含估算标注）+ Dashboard/用量报表页 | 单 provider 额度耗尽自动切换（误标记率≈0）；缓存降本；报表可按 key/模型/时间查看 |
| **M4（可选）** | 语义缓存、可观测面板、cost 路由策略、CSV 导出、PII/审核护栏 | 按需 |

> 建议 M0→M1 快速跑通，每个阶段先跑通单条再批量验证。

---

## 9. 风险与权衡

### 9.1 自建 vs 直接用 LiteLLM Proxy
| 维度 | LiteLLM Proxy | 本方案（FastAPI + LiteLLM SDK 自研治理层 + 控制台） |
|------|---------------|---------------------------------------------------|
| 上手速度 | 最快 | 需写接入/治理/前端代码 |
| 治理可控性 | 中 | 高（鉴权/限流/计量/缓存/配额标记全自定义） |
| 控制台 | 自带基础 UI | 自研 React 控制台（贴合本项目 IA） |
| 适合 | 想快点用 | 想完全掌控、含账号体系与配额感知路由 |

**建议**：个人/小团队只想验证，可先跑 LiteLLM Proxy；需深度定制（账号打通、配额感知路由、自有控制台）走本方案。两者都基于 LiteLLM，迁移成本低。

### 9.2 主要风险
- **配额误标记**（最高优先）：通用 429 与 quota 429 混淆会漏标/误标 → 仅靠错误 code 判定，误标记率目标 ≈ 0，QA 用 mock provider 端到端验（真耗尽切得动、健康不误标）。
- **token 计量偏差**：非 OpenAI 依赖近似计数，报表标「估算」，关键账单以 provider 为准。
- **RBAC 形同虚设**：若只前端隐藏菜单而不后端过滤，user 可越权 → 后端为真相源，QA 覆盖越权用例。
- **env 重启摩擦**：新增 provider 需改 env+重启，发布流程须记录（Phase 2 评估加密入库热加载）。
- **流式错误体验**：流中途失败须补发错误 event，禁止静默断流。
- **单点**：单机 Redis/网关故障影响全部流量，小团队可接受，后期加冗余。

---

## 10. 参考
- LiteLLM：https://docs.litellm.ai
- One API（Go，虚拟 key/额度）：https://github.com/songquanpeng/one-api
- APISIX AI Gateway：https://apisix.apache.org
- OpenAI API 规范：https://platform.openai.com/docs/api-reference
- Portkey / Helicone（可观测 + 路由增强，可作补充）
- 关联：`../product/PRD.md`（需求）、`../product/PRODUCT_DESIGN.md`（前端设计）、`../product/USER_STORIES.md`（用户故事）、`M0_DEV_PLAN.md` / `M1_DEV_PLAN.md`（开发计划）、`../meetings/PRD_REVIEW_MEETING.md` / `../meetings/PRODUCT_DESIGN_REVIEW_MEETING.md` / `../meetings/USER_STORIES_REVIEW_MEETING.md` / `../meetings/M0_DEV_PLAN_REVIEW_MEETING.md` / `../meetings/M1_DEV_PLAN_REVIEW_MEETING.md`（评审决议）
