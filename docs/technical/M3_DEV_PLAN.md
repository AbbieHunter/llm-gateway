# M3 开发计划（Dev Plan）· 配额感知路由 / fallback / 熔断 / 探活 / 精确缓存 / 成本报表

> 版本：v0.2（评审回写）
> 日期：2026-07-24
> 范围：M3 里程碑 —— 配额感知路由（错误码映射 + quota_exhausted 标记 + 探活）、fallback/重试/熔断、精确缓存、成本报表（按 Key / 模型 / 时间三维）、Dashboard 概览、估算标注与软扣减
> 关联：`../product/USER_STORIES.md`（US-M3-01~12，14 条）、`../product/PRD.md` §4.3/§4.5、`../product/PRODUCT_DESIGN.md`（Dashboard / 用量报表页）、`ARCHITECTURE.md` v0.2 §4.4/§4.5/§6.1/§6.3/§6.5、`M2_DEV_PLAN.md`（前置：用量落库 / 每日额度 / 流式错误补发接缝）
> 评审：见 `../meetings/M3_DEV_PLAN_REVIEW_MEETING.md`，8 项决议 R1~R8 已回写本节。

---

## 1. 范围与目标

M3 把 M0/M1/M2 跑通的「接入 + 账号 + 流式 + 额度 + 用量」补上**高可用与可观测的闭环**：

1. **配额感知路由**：某 provider 额度耗尽 → 精确识别（靠错误 code，不靠 HTTP 状态）→ 标记 + 自动切同别名下一可用 provider，调用方无感；误标记率目标 ≈ 0。
2. **韧性（重试 / 熔断 / fallback）**：瞬时故障退避重试；连续失败率超阈熔断防重试风暴；全失败返 `502` + 各候选失败明细。
3. **自愈（探活 / 手动重置）**：后台低频探活自动清除误/恢复标记；管理员一键重置。
4. **降本（精确缓存）**：相同非流式请求命中缓存零上游成本。
5. **可观测（成本报表三维 + Dashboard）**：按 Key / 模型 / 时间聚合的成本报表；Dashboard 四指标卡 + 近期异常列表。

M3 覆盖用户故事 **US-M3-01 ~ US-M3-12（共 14 条，全部 Must 除 US-M3-04=Should、US-M3-09=Should）**。

**M3 完成目标（一句话）**：主 provider 额度耗尽时开发者零感知切到备用；瞬时抖动自愈；误标一个都没有；报表能说清"钱花在哪、哪个 Key 在烧"。

---

## 2. 技术设计

> 关键前提：M2 已落地 `core/errors.py` 接缝（`GatewayError` + `headers` + `to_openai_error_body`）、`core/usage.py`（usage 落库）、`core/quota.py`（每日额度 Redis 计数）、`core/redis_client.py`（统一 Redis 客户端）。M3 在这些之上构建，不重复造轮子。

### 2.1 错误码映射（US-M3-01 / US-M3-03）

**落点 `core/errors.py`**：新增 `classify_error(exc, provider=None) -> ErrorCategory`，返回四类内部类别：
- `QUOTA_EXHAUSTED`：OpenAI `insufficient_quota`；DeepSeek `402 insufficient_balance`；通义 `QuotaExceeded` / `AccountBalanceInsufficient`。
- `RATE_LIMITED`：通用 429（非上述 quota code）。
- `AUTH_ERROR`：401/403（错误 key，不重试不标记）。
- `UPSTREAM_5XX` / `TIMEOUT`：超时 / 5xx。

**判定依据（已实读验证，非猜测）**：评审会前已实读 litellm 1.93 异常结构，字段**充足**，可直接分类：
- `RateLimitError`（及 `ServiceUnavailableError`/`AuthenticationError`/`BadRequestError`/`InternalServerError`）携带：`llm_provider`、`category`（`RateLimitErrorCategory` 枚举）、`response`（httpx.Response，可 `.json()` 取原始 body）、`message`；实例 `__dict__` 另有 `body`/`code`/`type` 兜底。
- `APIError`（DeepSeek 402 走这条）携带：`status_code`、`llm_provider`、`message`。
- `Timeout` 携带 `exception_status_code`。

**分类逻辑（R1 拍定）**：
1. `provider = exc.llm_provider`（优先）或传入 provider。
2. `status = exc.response.status_code`（若有）否则 `exc.status_code` 或 `exc.exception_status_code`。
3. `body = exc.response.json()`（若有）否则 `exc.body`；`message = exc.message`。
4. 按 `status` + `body.error.code` / `message` 子串判定：
   - `status in (401,403)` 或 `type/code == auth` → `AUTH_ERROR`。
   - `status == 402` 或 `body/message` 含 `insufficient_balance` / `QuotaExceeded` / `AccountBalanceInsufficient` / `insufficient_quota` → `QUOTA_EXHAUSTED`。
   - `status == 429`（且非上述 quota code）→ `RATE_LIMITED`。
   - `status >= 500` 或 `Timeout` → `UPSTREAM_5XX` / `TIMEOUT`。
5. **关键**：`RateLimitErrorCategory` 枚举值仅为 `VENDOR_RATE_LIMIT` / `VENDOR_BATCH_RATE_LIMIT` / `LITELLM_RATE_LIMIT` / `LITELLM_BATCH_RATE_LIMIT`，**无 quota 专属类别** → quota 识别**必须查原始 body/message 子串**，不能偷懒只看 `category`（R1）。

> **误标记红线（US-M3-03）**：仅当错误 code 明确指示 quota 才标 `quota_exhausted`；通用 429/5xx/超时**绝不**推断 quota。健康指标"配额误标记率"目标 ≈ 0，QA 用 mock 端到端验（真耗尽切得动、健康不误标）。

### 2.2 配额感知路由 + fallback 链（US-M3-02 / US-M3-08）

**Provider 运行时状态存 Redis**（沿用 ARCHITECTURE §4.5 决议，不落关系库）：
- 键：`provider_status:{provider_id}`，值 ∈ `healthy | degraded | down | quota_exhausted`。
- `core/router.py` 的 `resolve` 在 M1「过滤 disabled / 非 healthy」基础上，**新增过滤 `quota_exhausted` 与 `degraded`（熔断中）** 的候选。

**fallback 链语义（M1 朴素顺序试候选升级为带分类的 fallback）**：
```
attempt on primary candidate
 ├─ success → 返回
 ├─ QUOTA_EXHAUSTED → 标 quota_exhausted（写 Redis）+ 跳过该候选 + 试下一候选（不重试，重试无意义）
 ├─ RATE_LIMITED / UPSTREAM_5XX / TIMEOUT（retryable）
 │      ├─ 同候选退避重试 ≤ N（指数退避+抖动）→ 成功返回
 │      └─ 仍失败 → 标 degraded（写 Redis）+ 试下一候选
 ├─ AUTH_ERROR → 不重试不标记，计为候选失败原因
 └─ 全部候选失败/不可用 → 返回 502 + 各候选失败原因明细（US-M3-08，用户红线"分清网关侧 vs 自身代码"）
```

**流式 + fallback 的已知约束**：M2 的 `_stream_response` 目前是"首候选失败 naive 试下一"。M3 升级为：
- **首 token 之前**失败（流式首 chunk 抛错）→ 可无缝切下一候选（与非流式同逻辑）。
- **首 token 之后**中途错误 → 仍走 M2 的"补发 error event"逻辑（**无法中途无缝换 provider**，流式协议不允许），错误体 code 标明真实原因。文档明示此边界，不视为 bug。

### 2.3 可重试错误退避 + 熔断（US-M3-06 / US-M3-07）

- **退避（US-M3-06）**：仅对 retryable（超时 / 5xx / 限流 429）重试，指数退避 + 抖动，上限 **2~3 次**；**禁止对 4xx（除 429）重试**。
- **熔断（US-M3-07）**：per-provider 滑动窗口失败率超阈值 → 短暂熔断（R2：**阈值 = 失败率 + 窗口，做成全局可配项，默认 50% / 30s**；MVP 不暴露 per-provider 配置 UI）：
  - 熔断期间 `resolve` 跳过该 provider（等价于 `degraded`）。
  - 半开探测：熔断到期发一个探活请求，成功则恢复 `healthy`，失败续熔断。
  - 熔断计数/状态存 Redis（与 M2 额度同 Redis，便于横向扩展共享）。

### 2.4 探活自动恢复（US-M3-04，Should）

- 新增 `core/probe.py`：FastAPI `startup` 起一个 **asyncio 后台任务**（shutdown 取消），扫描 Redis 中 `quota_exhausted` 的 provider（R3：**周期可配，默认 10min**）。
- 对每个标记 provider 发 **tiny completion**（`max_tokens=1`，如 "hi"）用其 env 凭证（经 `core/adapters.py`）。
- 成功 → 清除标记回 `healthy`；仍 quota → 保持；**网络错不翻转为 `down`**（配额与网络是两个维度，架构 §4.5 红线）。
- **失败冷却（R3）**：单次探活失败则下次间隔指数退避（默认翻倍、上限 60min），避免对"真还透支"的 provider 疯狂重试把额度探没。
- **mock 端到端**：`MOCK_PROVIDER=1` 的 echo 适配器扩展 `?__quota=1` → 抛可被 `classify_error` 识别为 `QUOTA_EXHAUSTED` 的异常，供 T-04/T-05 无真 key 测探活与重置。

### 2.5 手动重置标记（US-M3-05）

- 后端：新增 `POST /api/providers/{id}/reset-status`（admin only）→ 清除该 provider 的 Redis 状态标记回 `healthy`。
- 前端：Dashboard「近期异常」列表（见 §2.8）展示被标 provider + 重置按钮；Provider 页也加重置入口。
- 重置后标记清除，下一请求该 provider 重新进入候选。

### 2.6 精确缓存（US-M3-09，Should）

- 新增 `core/cache.py`：仅作用于**非流式、确定性 prompt**。
- 缓存键：`hash(model + normalized(messages + temperature + top_p + seed))`（R7：归一化**必须包含影响输出的参数** `messages`/`temperature`/`top_p`/`seed`；`stream` 字段不入键，且缓存仅服务非流式，故无歧义）。
- 命中：直接返回缓存的 OpenAI 响应 JSON，**零上游成本**；TTL 可配（默认如 1h）。
- 与 M2 流式路径互不干扰（流式不查缓存）。

### 2.7 成本报表三维升级（US-M3-10a / b / c）

- 扩展 M2 的 `GET /api/usage`：新增 `group_by=key|model|time` 与 `range=day|week|month` 参数（R5：**`group_by` 默认 `key`；`range` 默认近 7 天**）。
  - `by_key`：按虚拟 Key 聚合（调用次数 / tokens / 花费 / 错误率），US-M3-10a（**默认视图，最受 admin 关心的"谁在烧"**）。
  - `by_model`：按模型 / 别名聚合，US-M3-10b。
  - `by_time`：按日 / 周 / 月时间范围，作用于 10a/10b，US-M3-10c。
- **RBAC 不变**：user 仍仅见自身 VK（后端过滤真相源，复用 M2 越权 403 逻辑）。
- 非 OpenAI 金额列继续标「估算」徽标（沿用 M2 `cost_is_estimated`）。
- 前端用量报表页用 tab 切换 `by_key` / `by_model` / `by_time`，默认 `key` 视图。

### 2.8 Dashboard 概览（US-M3-11）

- 后端：新增 `GET /api/dashboard/overview`，返回四指标卡数据（R6：今日调用量 / 今日花费 / **错误率 = 当日 `status≠success` 调用占比** / 活跃 Key 数（北极星 MAK 当日视图））；外加「近期异常」列表（读 Redis 中 `quota_exhausted` / `degraded` / `down` 的 provider，含 provider id 与状态）。
- **「配额误标记率」是独立健康指标（§5），不在四卡内**，仅在「近期异常」列表体现（被标 provider 数）。
- 前端：Dashboard 页加四指标卡 + 下方「近期异常」列表（含 §2.5 的重置按钮）；空状态引导"先创建虚拟 Key"。

### 2.9 估算标注与软扣减（US-M3-12）

- M2 已实现：`cost_is_estimated` 标注 + 额度扣减用实际 `prompt+completion` token。
- M3 **确认并细化**：非 OpenAI / 上游无 `usage` 时，估算 token **仅报表展示，不用于硬性扣减**（扣减以实际返回 token 优先；无 usage 时按保守估算且不在超额拦截中算"已用满"）。
- 界面/报表明确标「估算」，关键账单以 provider 为准（沿用 PRD 决议）。

### 2.10 依赖与前置

- **必须前置（已交付）**：US-M1-06（VK 鉴权 + account_id 注入）、US-M1-11（别名路由，提供候选列表）、US-M1-12（错误归一化接缝）、US-M1-13（fallback 骨架）、US-M2-04（每日额度 Redis 计数）、US-M2-06（usage 落库，报表数据源）、US-M2-07（用量视图 RBAC）。
- **M3 新增模块**：`core/cache.py`、`core/probe.py`（ARCHITECTURE §7 已规划）、`core/router.py` 增强（配额过滤 + fallback + 熔断）、`core/errors.py` 增强（`classify_error`）。
- **复用**：统一 Redis 客户端（`core/redis_client.py`）、`core/adapters.py` mock echo、M2 流式错误补发接缝。

### 2.11 关键设计决定（评审会 R1~R8 回写，已确认）

| 编号 | 决定 | 说明 |
|------|------|------|
| R1 | 错误分类字段（已实读验证） | `classify_error` 以 `exc.llm_provider` 定 provider，以 `status_code`（+`exception_status_code` 兜底）+ `response.json().error.code` / `message` 子串判定四类；quota 识别**必须查原始 body/message**，不依赖 `RateLimitErrorCategory` 枚举（其无 quota 专属值）；MVP 覆盖 OpenAI/DeepSeek/通义。 |
| R2 | 熔断阈值可配 | 失败率 + 窗口做成全局可配项（config），默认 50% / 30s；MVP 不暴露 per-provider 配置 UI。 |
| R3 | 探活周期 + 冷却 | 周期可配（默认 10min）；失败冷却指数退避（翻倍、上限 60min）；仅对 `quota_exhausted` 探活，网络错不翻转为 down。 |
| R4 | 流式 fallback 边界（验收二分） | 首 token 前失败 → 切候选成功；首 token 后错误 → 补发 error event（边界，非 bug）；非流式走完整 fallback 链。 |
| R5 | 用量报表默认视图 | `group_by` 默认 `key`（调用/tokens/花费/错误率），模型/时间切换 tab；`range` 默认近 7 天；后端默认 `key`；RBAC 沿用。 |
| R6 | Dashboard 口径 | 「错误率」= 当日 `status≠success` 调用占比；「配额误标记率」为独立健康指标不在四卡内（仅近期异常列表体现）。 |
| R7 | 精确缓存键范围 | 归一化键含 `model + messages + temperature + top_p + seed`；`stream` 不入键且缓存仅非流式；TTL 默认 1h 可配。 |
| R8 | 文档回写 | ARCHITECTURE §4.4/§5 `quota_policy` JSON → `daily_token_quota INT NULL` 列（与 M2 一致）；§5 删 `expires_at`（M1 R6）；§4.5 补"provider 状态存 Redis，键 `provider_status:{id}`，不落关系库"。 |

> 沿用架构决议：Provider 运行时状态（healthy/degraded/down/quota_exhausted）**存 Redis**（键 `provider_status:{id}`），不落关系库（R-arch-1）；熔断/重试/探活全部走 Redis 状态，与 M2 额度同 Redis 实例（R-arch-8）。这两项在评审中无异议，直接保留。

---

## 3. 开发任务拆解（T-01 ~ T-10）

> T 恤尺码（S/M/L）为**建议值，待 Dev 在 M3 评审确认**。每个任务含可勾选子项、对应故事、依赖。

### T-01 · 错误码映射 classify_error — 【L】
- 对应：US-M3-01 / US-M3-03
- 依赖：US-M1-12（errors.py 接缝）、LiteLLM 异常结构（**已实读验证字段充足**，见 §2.1）
- [ ] `classify_error(exc, provider=None) -> ErrorCategory` 四类实现（R1：取 `llm_provider` + `status_code`/`exception_status_code` + `response.json().error.code`/`message` 子串；quota 查 body/message 不查 category 枚举），MVP 覆盖 OpenAI/DeepSeek/通义
- [ ] mock 适配器扩展 `?__quota=1` → 抛可被分类为 QUOTA_EXHAUSTED 的异常（建议抛 `RateLimitError`，message 含 `insufficient_quota` 风格，便于端到端）
- [ ] 单测：三家 quota code 命中 QUOTA_EXHAUSTED；通用 429/5xx/超时**不**命中 quota（误标记红线）
- 验收：真实异常（已用 litellm 1.93 实例验证字段）与 mock 异常均被正确分类；健康模型不因通用 429 被误标

### T-02 · 配额感知路由 + fallback 链 + 502 明细 — 【L】
- 对应：US-M3-02 / US-M3-08
- 依赖：T-01、US-M1-11（router 候选）、US-M1-13（fallback 骨架）
- [ ] Redis 状态键 `provider_status:{id}` 读写封装（redis_client 扩展）
- [ ] `resolve` 新增过滤 `quota_exhausted` / `degraded`
- [ ] 非流式 `_nonstream_response` 升级为带分类的 fallback 链（QUOTA_EXHAUSTED 标记+换候选 / retryable 退避重试 / AUTH 计失败原因）
- [ ] 全失败 → 502 + 各候选失败原因明细（US-M3-08 用户红线）
- [ ] 流式 `_stream_response` 首 token 前失败可 fallback（R4：**首 token 后错误沿用 M2 补发 error event，不要求无缝切换，验收明确二分**）
- 验收：mock 主 provider 抛 quota → 透明切备用返回；全失败 502 含明细；健康不误标

### T-03 · 可重试退避 + 熔断 — 【L】
- 对应：US-M3-06 / US-M3-07
- 依赖：T-02、Redis 可用
- [ ] retryable 退避重试（指数+抖动，≤2~3 次），禁止 4xx（除429）重试
- [ ] per-provider 熔断滑动窗口（R2：阈值 = 失败率+窗口，**全局可配，默认 50%/30s**），状态存 Redis
- [ ] 熔断期 `resolve` 跳过；半开探测恢复
- 验收：连续 5xx 触发熔断后跳过该 provider；半开恢复；无重试风暴（探活/重试计数可见）

### T-04 · 探活 probe 后台任务 — 【M】
- 对应：US-M3-04
- 依赖：T-02（状态写 Redis）、core/adapters.py
- [ ] `core/probe.py` 后台 asyncio 任务（startup 起 / shutdown 取消）
- [ ] 周期扫 quota_exhausted provider，发 tiny completion（max_tokens=1）（R3：**周期可配，默认 10min**）
- [ ] 成功→清标记回 healthy；网络错不翻转为 down
- [ ] **失败冷却指数退避（翻倍、上限 60min）**，避免自耗额度/触发限流
- 验收：mock quota provider 经探活自动恢复 healthy；网络错保持标记；失败冷却生效

### T-05 · 手动重置标记 API + 入口 — 【M】
- 对应：US-M3-05
- 依赖：T-02
- [ ] `POST /api/providers/{id}/reset-status`（admin only）→ 清 Redis 标记回 healthy
- [ ] Provider 页加重置入口（前端）
- 验收：被标 provider 重置后立即重新进入候选；非 admin 调返回 403

### T-06 · 精确缓存 cache.py — 【M】
- 对应：US-M3-09
- 依赖：Redis 可用、non-stream 路径
- [ ] `core/cache.py`：键 = hash(model + 归一化请求)，仅非流式确定性 prompt（R7：**归一化含 messages/temperature/top_p/seed；`stream` 不入键**）
- [ ] 命中直接返回缓存响应，TTL 可配（默认 1h）
- [ ] 非流式路径接入查/写缓存
- 验收：相同请求二次调用零上游（mock 计数/日志可见不调上游）；流式不查缓存；不同 temperature 不命中同一缓存

### T-07 · 用量报表三维升级 — 【L】
- 对应：US-M3-10a / b / c
- 依赖：US-M2-06 / US-M2-07（/api/usage 基线）、RBAC
- [ ] `/api/usage` 扩展 `group_by=key|model|time` + `range=day|week|month`（R5：**默认 `key` / 近 7 天**）
- [ ] by_key（调用/tokens/花费/错误率）、by_model（含别名）、by_time（日/周/月）
- [ ] RBAC 越权 403 沿用 M2 逻辑
- 验收：三维度聚合正确；默认按 Key 视图；非 OpenAI 标估算；user 仅见自身

### T-08 · Dashboard 概览 — 【M】
- 对应：US-M3-11
- 依赖：T-02（Redis 状态）、US-M2-06（数据源）、T-05（重置入口）
- [ ] `GET /api/dashboard/overview`：今日调用量/花费/**错误率(status≠success 占比)**/活跃 Key 数 + 近期异常列表（R6）
- [ ] Dashboard 前端四指标卡 + 近期异常列表（含重置按钮）+ 空状态引导
- 验收：指标卡数值正确（错误率口径正确）；异常列表展示被标 provider 且重置生效

### T-09 · 估算标注与软扣减确认 — 【S】
- 对应：US-M3-12
- 依赖：US-M2-04 / US-M2-06
- [ ] 确认额度扣减以实际 token 优先；非 OpenAI 无 usage 估算仅报表、不硬性扣减
- [ ] 报表/界面「估算」标注一致（复用 M2 cost_is_estimated）
- 验收：估算值不导致 Key 被误封；界面标注清晰

### T-10 · 集成测试与冒烟扩展 — 【L】
- 对应：US-M3-01~12（QA 用例落地）
- 依赖：T-01~T-09、MOCK_PROVIDER=1
- [ ] pytest：错误码映射（三家 quota + 误标记红线）、配额感知 fallback（切得动）、502 明细、重试/熔断、探活恢复、手动重置、精确缓存命中、报表三维、Dashboard 异常列表、估算软扣减
- [ ] `scripts/smoke.sh` 扩展 M3 用例（配额耗尽透明切换、全失败 502）
- [ ] 复用 M1/M2 mock echo（扩展 `?__quota=1`）做无 key 端到端
- 验收：CI 可跑；覆盖 US-M3-01/02/03（配额三连，含误标记）、US-M3-08（502 明细）重点

### 依赖关系
```
US-M1-11/12/13 (前置)
   └─▶ T-01 (错误映射, 含 spike) ──▶ T-02 (配额路由+fallback+502)
                                      ├─▶ T-03 (重试+熔断)
                                      ├─▶ T-04 (探活) ──▶ T-05 (手动重置)
   T-02 ──▶ T-08 (Dashboard 异常, 需 Redis 状态)
   US-M2-06/07 ──▶ T-07 (报表三维)
   Redis + non-stream ──▶ T-06 (精确缓存)
   US-M2-04/06 ──▶ T-09 (估算软扣减)
   T-01~T-09 ──▶ T-10 (测试/冒烟)
```
> T-01 与 T-06 可并行；T-04/T-05 与 T-03 可并行；T-10 收口。

---

## 4. M3 完成定义（Definition of Done）

- [ ] 主 provider 额度耗尽（mock `?__quota=1`）→ 请求透明 fallback 到同别名下一可用 provider，调用方无感
- [ ] 错误分类按 code 而非 HTTP 状态；通用 429/5xx/超时**不**触发 quota_exhausted（误标记率 ≈ 0）
- [ ] retryable 错误退避重试（≤2~3 次，指数+抖动）；4xx（除429）不重试
- [ ] per-provider 熔断：连续失败率超阈短暂熔断并跳过；半开自动恢复；无重试风暴
- [ ] 候选全失败 → 502 + 各候选失败原因明细
- [ ] 后台探活周期清除 quota_exhausted 标记（网络错不翻转为 down）
- [ ] 管理员可一键重置被标 provider（API + Dashboard/Provider 页）
- [ ] 精确缓存：相同非流式请求命中零上游成本；流式不查缓存
- [ ] 用量报表支持按 Key / 模型 / 时间（日/周/月）三维；user 仅见自身
- [ ] Dashboard 四指标卡 + 近期异常列表（含重置）可用
- [ ] 非 OpenAI 估算 token 仅报表、不硬性扣减；界面标「估算」
- [ ] pytest 覆盖 M3 重点故事（配额三连 / 502 明细 / 熔断 / 探活）；`scripts/smoke.sh` 扩展通过
- [ ] 架构 §4.4/§5 `quota_policy` → `daily_token_quota` 等文档漂移回写对齐（R-arch-9）

### 非目标（M3 显式不做）
- 语义缓存（M4）
- cost 路由策略（M4）
- CSV 导出 / 趋势图（MVP 显式不做，沿用决议）
- PII / 审核护栏（Phase 2）
- 跨 provider 金额硬限额（仅报表，PRD 决议）
- 细粒度 QPS / 并发限流（后置）

---

## 5. 风险与注意

- **LiteLLM 异常结构不确定（最高风险）**：`classify_error` 依赖 litellm 暴露底层 provider 原始 code/body；litellm 1.93 把"缺凭证"包成 `InternalServerError`，归一化程度未知。**T-01 第一步必须实读异常属性取证**，不可凭记忆猜（见 §2.1 spike 与用户记忆铁律）。
- **误标记红线**：通用 429 与 quota 429 混淆会漏标/误标 → 仅靠错误 code 判定，QA 用 mock 端到端验（真耗尽切得动、健康不误标）。
- **熔断/探活状态重启丢失**：Redis 重启丢熔断窗口与探活进度（短暂），可接受；单进程内共享，横向扩展同 Redis。
- **探活自耗额度**：tiny completion 仍消耗真实额度 / 可能触发限流 → 频率+冷却必须保守，mock 端到端验证。
- **流式 fallback 边界**：首 token 后中途错误无法无缝换 provider（协议限制），沿用 M2 补发 error event + 文档明示，不视为 bug。
- **精确缓存命中错乱**：归一化需排除随机/非确定性字段（seed 等），避免不同 prompt 命中同一缓存；TTL 内 provider 价格变动不影响（仅降本）。
- **依赖纪律**：延续 M1/M2 铁律——新增依赖优先纯 Python，避开 3.13 原生包坑（litellm 已钉 `>=1.67,<2.0`）。
- **文档回写**：§2.11 R-arch-9 的 `quota_policy`→`daily_token_quota` 漂移需在 M3 评审通过后回写 `ARCHITECTURE.md`，保持文档链一致。
