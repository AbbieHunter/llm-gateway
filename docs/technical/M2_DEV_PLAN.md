# M2 开发计划（Dev Plan）· 流式 SSE / 每日 token 额度限流 / 用量日志与归因

> 版本：v0.2
> 日期：2026-07-24
> 范围：M2 里程碑 —— 流式调用 + 流式错误补发 + 客户端断开处理 + 每日 token 额度限流 + 自然日重置 + 用量落库与归因 + 用量视图
> 关联：`../product/USER_STORIES.md`（US-M2-01~07）、`../product/PRD.md` §4.3/§4.5、`../product/PRODUCT_DESIGN.md`（用量报表页）、`ARCHITECTURE.md` v0.2 §4.7/§6.1/§6.2、`M1_DEV_PLAN.md`（前置：VK 鉴权 / 别名路由 / 虚拟 Key 表）

---

## 1. 范围与目标

M2 把 M0/M1 跑通的「非流式单/多 provider 调用 + 账号体系」补上**三块能力**：

1. **流式对话（SSE）**：`stream=true` 时逐块转发，体验不差于直连。
2. **成本护栏（每日 token 额度）**：按 VK 设每日 token 硬限额，超限 `429 + Retry-After`，本地时区自然日重置。
3. **可观测底座（用量日志与归因）**：每次调用落 `usage_logs`，成本可归因到 VK / 账号 / 模型 / provider，控制台可查。

M2 覆盖用户故事 **US-M2-01 ~ US-M2-07（共 7 条，全部 Must）**。

**M2 完成目标（一句话）**：开发者用真实 SDK 经 gateway 流式对话不卡顿；超额度被干净拦截；每一分 token 花在谁身上都能查到。

---

## 2. 技术设计

### 2.1 流式调用（US-M2-01）

**设计决定：流式沿用 M1 的「LiteLLM 直连」决策**，不单独引 `httpx.AsyncClient.stream()`。
架构 §6.2 写的 `httpx.AsyncClient.stream()` 是 M0 早期设想，M1 已确定网关经 `litellm.acompletion(...)` 直连，流式是同一入口的 `stream=True` 形态。M2 据此定稿，**架构 §6.2 的 httpx 描述待回写**（见 §2.9 R-arch）。

- 请求：`litellm.acompletion(model=..., messages=..., stream=True, **kwargs)` 返回 **异步迭代器**（LiteLLM `AsyncCustomStreamWrapper`）。
- 重帧为 SSE：
  ```python
  async def sse_generator():
      async for chunk in stream:
          yield f"data: {chunk.model_dump_json()}\n\n"
      yield "data: [DONE]\n\n"
  return StreamingResponse(sse_generator(), media_type="text/event-stream")
  ```
- **tool call 透传**：LiteLLM 的 chunk 已含 `tool_calls` delta，`model_dump_json()` 直接保留，无需额外处理（满足 US-M2-01 "含 tool call 透传"）。
- 路由别名在 M1 已接入（`adapter.chat` 解析别名 → 候选列表 → 选首选）。M2 仅把 `stream` 透传给同一 `adapter.chat`。

### 2.2 流式错误补发（US-M2-02）

- 在 `sse_generator` 内 `try/except Exception` 包住 `async for`：
  - 上游流中途抛错 → 用 `core/errors.py` 的映射生成 OpenAI 风格错误体 → `yield f"data: {json.dumps(err_body)}\n\n"` → `return`（正常结束流，客户端收到错误 event 而非半截静默）。
  - **禁止**直接 `raise`：那样会让 `StreamingResponse` 在中途断流，客户端看到 "连接重置" 而非结构化错误。
- 错误体沿用 M0 已种的 `to_openai_error(exc)` 接缝，HTTP 语义通过 event 内的 `code`/HTTP 状态在首部体现（首部状态在流开始已定，错误 event 内 `code` 兜底）。

### 2.3 客户端断开处理（US-M2-03）

- Starlette 在客户端断开时会向 `StreamingResponse` 的生成器任务抛 `asyncio.CancelledError`。
- 生成器 `try/except CancelledError`：
  - 调用 `await stream.aclose()`（best-effort，关闭上游 httpx 连接）停止继续消耗上游 token；
  - 记录一条 `usage_logs`：`status='client_disconnect'`，token 记已知部分（prompt_tokens + 已累计 completion_tokens 近似值），`cost_is_estimated=True`；
  - **[R1] 同时 `INCRBY` Redis 配额计数器 partial tokens**（与正常结束走同一条计数路径），否则客户端可反复"断开 dodge 限额"——已烧的 token 必须计入当日额度。
- 额外保险：生成器每轮 `await request.is_disconnected()` 探测（Starlette `Request` 注入），已断开则主动 `break` 并走上述收尾。
- **优先级 Must（评审决议）**：用户原话"断流还在烧 token 我零容忍"，故必须 cancel，不能靠 GC 回收。

### 2.4 每日 token 额度限流（US-M2-04）

- **Redis 必需**：M1 把 Redis 当可选（未配则健康全 `healthy`）；M2 额度计数必须落 Redis，**启动对 Redis 做 `PING` 探活，连不上直接 fail-loud**（[R4] 仅校验 `REDIS_URL` 存在不够，有 URL 不等于能连）；启动日志提示"计数仅存 Redis、重启会丢当日计数"已知限制。`docker-compose.yml` 从 M2 起**重新挂 redis**（M0 曾去掉，现恢复）。
- VK 加列：`virtual_keys.daily_token_quota INT NULL`（M1 建的表上做 **additive 迁移**，不破坏既有数据）。`NULL` = 不限制。
- 计数键：`quota:{vk_id}:{YYYY-MM-DD}`（日期取**本地时区**）。
- 判定逻辑（请求门）：
  1. 取 VK `daily_token_quota`；为 `NULL` → 跳过限流。
  2. 取 `current = redis.get(key)`；若 `current` 非空且 `int(current) >= quota` → 返 `429` + `Retry-After`（= 距本地次日 00:00 的秒数）。
  3. 调用上游，结束/异常/断开后 `redis.incrby(key, prompt_tokens + completion_tokens)`；键不存在则 `SET` 并 `EXPIRE(key, 距本地午夜秒数)`。
- **软硬说明（接受的行为）**：**计数基于历史累计值（上一请求结束后的总和），不含当前进行中的请求**。判定发生在调用前，无法预知 completion token，故单条超大请求可能轻微超过额度上限（[R5] 在此加粗明示）。MVP「硬限额」定义为"超限后**下一个请求**即被拦"，不拦截进行中的请求。文档标注此行为，避免误读为"绝对不超 1 token"。

### 2.5 自然日重置（US-M2-05）

- Redis 键 TTL 对齐**本地时区次日 00:00**：`ttl = (next_local_midnight - now).total_seconds()`。
- 跨日第一次调用计数从 0 起（`GET` 键已过期返回 `None`）。
- `429` 响应的 `Retry-After` = 同一 `ttl`，前端可展示"还剩 X 小时重置"。

### 2.6 用量落库与归因（US-M2-06）

- 表 `usage_logs`（schema 见 `ARCHITECTURE.md` §5，已建）。
- 写入时机：非流式在响应后；流式在 `[DONE]` 或异常/断开后。
- 字段填充：
  - `vk_id` / `account_id`：来自 VK 鉴权中间件注入的上下文（M1）。
  - `model`：实际使用的 LiteLLM 模型串（别名路由时为**实际选中候选**，如 `openai/gpt-4o-mini`）。
  - `provider`：`model.split('/')[0]`；无 `/` 时默认 `openai`（直连裸模型名按 OpenAI 处理）。
  - `prompt_tokens` / `completion_tokens`：来自响应 `usage`（流式取末块 `usage`；断开取已知部分）。
  - `cost_usd` / `cost_is_estimated`：优先 `litellm.completion_cost(response)`；非 OpenAI 或计算失败 → `cost_is_estimated=True`，值尽量估算（仅报表，不硬限额）。
  - `latency_ms`：请求发起到响应首/末字节。
  - `status`：`success` / `error` / `client_disconnect` / `rate_limited`。
- 索引：`idx_usage_account_time`、`idx_usage_vk_time`（架构已定义）覆盖归因查询。

### 2.7 用量视图（US-M2-07）

- 后端：`GET /api/usage`
  - User：后端强制 `filter_by_owner(account_id)`，仅见自身 VK。**[R2] `scope` / `global` / `account_id` 参数仅 Admin 有效；非 admin 传了越权参数直接忽略并返 `403`（记安全日志），绝不拼入查询**。
  - Admin：可加 `?scope=global` 看全局，或 `?vk_id=` 下钻。
  - 聚合维度：按日 / 按 VK / 按模型；返回 `{date, vk_id, model, provider, calls, total_tokens, cost_usd, cost_is_estimated}`。
- 前端：M1 已搭控制台骨架，M2 新增**「用量报表」页**（轻量表格，**不做趋势图/CSV**——属 MVP 显式不做，见 §4 非目标）。字段对齐上表；非 OpenAI 金额列标「估算」徽标。

### 2.8 依赖与前置

- **必须前置（M1 已交付）**：US-M1-06（VK 鉴权 + account_id 注入）、US-M1-11（别名路由，提供 provider 归因）、US-M1-04（虚拟 Key 表 + 控制台基础）、US-M1-05（后端 RBAC，用量视图权限真相源）。
- **M2 新增依赖**：Redis（运行必需）。
- **复用**：`core/errors.py`（M0 接缝）、`core/adapters.py`（`adapter.chat` 已支持 stream）、`MOCK_PROVIDER=1` echo（M1 引入，M2 流式测试复用，echo 支持 stream 形态）。

### 2.9 关键设计决定（R-arch 待回写架构）

| 决定 | 说明 |
|------|------|
| R-arch-1 | 流式用 **LiteLLM 原生 `acompletion(stream=True)`** 迭代器重帧 SSE；架构 §6.2 的 `httpx.AsyncClient.stream()` 描述**过时**，回写时改为 litellm 直连口径（与 M1 一致）。 |
| R-arch-2 | Redis 在 M2 起**必需**，compose 重新挂 redis；`REDIS_URL` 缺失 → 启动失败。 |
| R-arch-3 | 额度判定为「请求粒度软硬限额」，单请求可轻微超阈，下一请求拦截；文档明示。 |
| R-arch-4 | `provider` 字段由 `model.split('/')[0]` 推导；别名路由时取实际选中候选串。 |

---

## 3. 开发任务拆解（T-01 ~ T-08）

> T 恤尺码（S/M/L）为**建议值，待 Dev 在 M2 评审确认**。每个任务含可勾选子项、对应故事、依赖。

### T-01 · 流式 SSE 透传 — 【L】
- 对应：US-M2-01
- 依赖：US-M1-06、US-M1-11（adapter.chat 已支持 stream）
- [ ] `adapter.chat` 支持 `stream=True`，返回 LiteLLM 异步流
- [ ] `routers/openai.py` 新增 `StreamingResponse` 分支：逐块 `data: {chunk.model_dump_json()}\n\n`，结尾 `data: [DONE]\n\n`
- [ ] `MOCK_PROVIDER=1` echo 支持 stream 形态（[R3] `?__stream=1` 分块 + `?__error_after=N` 注入中途错误，供 T-02/T-03 无 key 测试）
- [ ] tool call delta 透传验证（mock 构造带 tool_calls 的 chunk）
- 验收：官方 SDK `stream=True` 经 gateway 收到连续 SSE，内容与直连一致；`/v1/models` 不受影响

### T-02 · 流式错误补发 — 【M】
- 对应：US-M2-02
- 依赖：T-01、core/errors.py（M0）
- [ ] 生成器 `try/except Exception`，上游中途报错 → 末块补发 `data: {"error": {...}}\n\n` 后正常结束
- [ ] 错误体经 `to_openai_error` 映射（复用 M0 接缝）
- [ ] 禁止静默断流（用例：mock 在发 3 块后抛错，客户端应收到第 4 块 error event）
- 验收：客户端不会"收到一半无后续"；error event 结构符合 OpenAI

### T-03 · 客户端断开 cancel — 【M】
- 对应：US-M2-03
- 依赖：T-01
- [ ] 生成器捕获 `asyncio.CancelledError` 与 `request.is_disconnected()`
- [ ] 断开即 `await stream.aclose()` 取消上游
- [ ] 写 `usage_logs`（`status='client_disconnect'`，partial token，`cost_is_estimated=True`）
- [ ] **[R1] 同时 `INCRBY` Redis 配额计数器 partial tokens**，与正常结束同路径
- 验收：客户端中途断开后，上游连接被关（mock 计数/日志可见不再推进）；不继续消耗 token；配额计数已含 partial（防 dodge）

### T-04 · 每日 token 额度（Redis + VK 列） — 【M】
- 对应：US-M2-04
- 依赖：US-M1-06（virtual_keys 表）、Redis 可用
- [ ] `virtual_keys` 加 `daily_token_quota INT NULL`（additive 迁移脚本）
- [ ] **[R4] 启动对 Redis 做 `PING` 探活**，连不上 fail-loud；日志提示"计数仅存 Redis、重启丢当日计数"
- [ ] 请求门：取配额 → `quota:{vk_id}:{date}` 超阈返 429 + Retry-After
- [ ] 调用后 `INCRBY` 实际 tokens（prompt+completion）
- [ ] 控制台 VK 编辑页加「每日 token 额度」输入框（NULL=不限制）
- 验收：设 100 token 配额，第二次超额调用返 429 且带 Retry-After；NULL 不限

### T-05 · 自然日重置 — 【S】
- 对应：US-M2-05
- 依赖：T-04
- [ ] 键 TTL = 距本地次日 00:00 秒数（首写时设置）
- [ ] `429` 的 `Retry-After` = 同一 ttl
- [ ] 跨日调用计数归零（键过期 `GET` 返 None）
- 验收：TZ=Asia/Shanghai 下，23:59 写入的键在次日 00:00 后过期；Retry-After 值合理

### T-06 · 用量落库与归因 — 【L】
- 对应：US-M2-06
- 依赖：US-M1-06（vk_id/account_id 上下文）、T-01（流式末块 usage）
- [ ] 每次调用（成功/错误/断开）写 `usage_logs`
- [ ] `provider = model.split('/')[0]`；无 `/` 默认 `openai`
- [ ] 非 OpenAI 或 cost 计算失败 → `cost_is_estimated=True`
- [ ] 流式末块取 `usage`；断开取已知 partial
- 验收：日志含 vk_id/account_id/model/provider/tokens/status；索引查询可用；非 OpenAI 标估算

### T-07 · 用量视图（API + 控制台页） — 【M】
- 对应：US-M2-07
- 依赖：T-06、US-M1-05（RBAC）、US-M1-04（控制台骨架）
- [ ] `GET /api/usage`（User 自见、Admin 可 global/vk_id 下钻）
- [ ] 聚合：按日/按 VK/按模型
- [ ] 控制台「用量报表」页（轻量表格，非 OpenAI 金额标「估算」；**无图表/CSV**）
- [ ] **[R2] 非 admin 传 `?scope=global` / `?account_id=` 越权参数 → 返 403 + 安全日志**，绝不拼入查询
- 验收：User 越权查他人 Key 被拦（后端过滤 + 403）；Admin 见全局聚合

### T-08 · 集成测试与冒烟扩展 — 【M】
- 对应：US-M2-01~07（QA 用例落地）
- 依赖：T-01~T-07、MOCK_PROVIDER=1
- [ ] `scripts/smoke.sh` 扩展：stream 真收 SSE、超额度 429、用量 API 返回
- [ ] pytest：流式错误补发（[R3] mock `__error_after`）、客户端断开 cancel（[R3] mock）、额度门、自然日 TTL、用量归因、RBAC 越权（[R2] 403）
- [ ] **[R7] 加 pytest 断言 SSE 原始字节**：`data: ` 前缀、`\n\n` 分隔、`data: [DONE]` 结尾
- [ ] 复用 M1 mock echo（[R3] 扩展后的 stream/error 形态）做无 key 端到端
- 验收：CI 可跑；覆盖 US-M2-02/03/04/07 重点

### 依赖关系
```
US-M1-06/11 (前置)
   └─▶ T-01 (流式) ──▶ T-02 (错误补发)
        └─▶ T-03 (断开 cancel)
   └─▶ T-04 (额度, 需 Redis) ──▶ T-05 (重置)
   └─▶ T-06 (用量落库) ──▶ T-07 (用量视图, 需 RBAC)
T-01~T-07 ──▶ T-08 (测试/冒烟)
```
> T-01 与 T-04 可并行；T-06 与 T-04 可并行；T-08 收口。

---

## 4. M2 完成定义（Definition of Done）

- [ ] 官方 OpenAI SDK `stream=True` 经 gateway 收到连续 SSE，`[DONE]` 正常结束，内容与直连一致（含 tool call）
- [ ] 流中途上游报错 → 客户端收到结构化 error event，无静默断流
- [ ] 客户端中途断开 → 上游连接被 cancel，不再消耗 token，落 `client_disconnect` 日志
- [ ] VK 设每日 token 额度后，超限下一请求返 `429 + Retry-After`；`NULL` 不限
- [ ] 额度按本地时区自然日重置；`Retry-After` 值合理
- [ ] 每次调用落 `usage_logs`（vk_id/account_id/model/provider/tokens/status），非 OpenAI 标估算
- [ ] 控制台「用量报表」页可用；User 仅见自身、Admin 见全局（后端过滤）
- [ ] Redis 缺失时 gateway 启动失败（fail-loud）；`docker-compose.yml` 重新挂 redis
- [ ] `scripts/smoke.sh` 扩展通过；pytest 覆盖 M2 重点故事
- [ ] 架构 §6.2 httpx 描述回写为 litellm 直连口径（R-arch-1）

### 非目标（M2 显式不做）
- 语义缓存（M3）
- 配额感知路由的错误码映射 / 额度耗尽标记 / 探活（M3）
- 成本**金额**硬限额（仅报表展示，MVP 决议）
- 细粒度 QPS 限流（后置）
- CSV 导出 / 趋势图（MVP 显式不做，见 `USER_STORIES.md` Won't 清单）

---

## 5. 风险与注意

- **Redis 重启丢计数器**：Redis 键 TTL 内重启会丢当日计数 → 限额短暂失效。MVP 接受（个人小团队），Postgres 持久化计数留 Phase 2。文档标注。
- **流式 final usage 缺失**：上游不总发末块 `usage`（尤其异常/断开），部分日志 token 为估算；`cost_is_estimated` 兜底。
- **LiteLLM 流 cancel 语义**：`AsyncCustomStreamWrapper.aclose()` 行为随版本变；T-03 需实测确认能真正断上游 httpx，必要时 `stream.response_iterator` 直接 `aclose`。
- **SSE 格式严格性**：双换行 `\n\n` 与 `data:` 前缀必须精确，否则 SDK 不解析；T-01 用例校验原始字节。
- **额度软超**：单请求可轻微超阈（§2.4），测试与文档明示，勿误报为 bug。
- **依赖纪律**：延续 M1 铁律——新增依赖优先纯 Python，避免 3.13 原生包坑（litellm 已钉 `>=1.67,<2.0`）。
- **架构回写**：§2.9 四项（尤其 R-arch-1 httpx→litellm）需在 M2 评审通过后回写 `ARCHITECTURE.md`，保持文档链一致。
