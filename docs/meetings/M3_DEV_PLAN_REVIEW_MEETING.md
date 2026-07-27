# M3 开发计划评审会纪要（模拟）

> 文档：`../technical/M3_DEV_PLAN.md` v0.1
> 日期：2026-07-24
> 参会：产品（PM）、开发（Dev）、测试（QA）、用户（实际使用者，团队里的开发者）
> 结论：**通过，回写 v0.2**（8 项决议 R1~R8）

---

## 🎬 会议实录

**PM**：M3 计划 v0.1 覆盖 US-M3-01~12 十四条故事，重点是配额感知路由 + 韧性 + 可观测。今天目标：把"错误怎么分、熔断/探活怎么踩边界、报表维度怎么定"这些最容易返工的点拍实。Dev 你先开刀。

### 议题 1：错误分类到底靠什么字段？

**Dev**：v0.1 把"LiteLLM 异常归一化字段未知"列成了最高风险，说 T-01 要先 spike。我**刚实读了一遍 litellm 1.93 的异常结构**，结论比预想乐观——字段是够的：
- `RateLimitError` 带 `llm_provider` + `category`（一个 `RateLimitErrorCategory` 枚举）+ `response`（httpx.Response，可 `.json()` 拿原始 body）+ `message`；
- `APIError`（DeepSeek 402 走这条）带 `status_code` + `llm_provider` + `message`；
- 实例 `__dict__` 里还有 `body` / `code` / `type` 兜底字段。

也就是说 `classify_error(exc)` 能直接拿到 **provider + status_code + 原始 body**，靠 `status_code` 配合 `body.error.code` / `message` 子串判定 quota，**不依赖 HTTP 状态**。最高风险解除，不用再手软。

**QA**：那 `category` 枚举有没有直接给 "quota" 语义？如果有，OpenAI 那条就不用解析 body 了。

**Dev**：枚举值是 `VENDOR_RATE_LIMIT` / `VENDOR_BATCH_RATE_LIMIT` / `LITELLM_RATE_LIMIT` / `LITELLM_BATCH_RATE_LIMIT`——**没有单独的 quota 类别**。所以 quota 仍得靠 `status_code` + `body.error.code`（`insufficient_quota`）/`message` 子串（`insufficient_balance` / `QuotaExceeded` / `AccountBalanceInsufficient`）判定。这点要明确写进 T-01，不能偷懒只查 `category`。

**PM**：好，把"已实读、字段够用、但 quota 必须查 body/message 子串而非 category"作为结论钉死。决议：**R1 — `classify_error` 以 `exc.llm_provider` 定 provider，以 `status_code`（+ `exception_status_code` 兜底）+ `response.json().error.code` / `message` 子串判定四类；quota 识别必须查原始 body/message，不依赖 `RateLimitErrorCategory` 枚举；MVP 覆盖 OpenAI/DeepSeek/通义**。T-01 去掉"spike 待定"口吻，改成"已验证字段可用，直接实现"。

### 议题 2：熔断阈值与可配性

**Dev**：§2.3 写"默认 50%/30s"。但不同 provider 抖动性格不同，OpenAI 偶尔 5xx 一阵子，DeepSeek 可能更稳。建议**阈值做成可配**（配置项或表字段），默认 50%/30s，先全局一把，后续可细化到 provider。

**QA**：同意，但 MVP 先全局默认值即可，别在 M3 搞 per-provider 配置 UI，那是 M4 的事。

**PM**：决议：**R2 — 熔断阈值（失败率 + 窗口）做成全局可配项（config，默认 50%/30s），MVP 不暴露 per-provider 配置 UI**；状态存 Redis，重启丢窗口可接受。

### 议题 3：探活周期与冷却

**Dev**：§2.4 写"~10min，带冷却"。tiny completion 虽小但走真实 key、消耗真实额度，频率高了可能反而把自己限流。建议周期**可配、默认 10min**，且单次探活失败要有冷却（如本次失败则下次间隔翻倍，上限 60min），避免对"真还透支"的 provider 疯狂重试。

**用户**：对，别为了探活把额度探没了。

**PM**：决议：**R3 — 探活周期可配（默认 10min）；失败冷却指数退避（默认翻倍、上限 60min）；仅对 `quota_exhausted` 探活，网络错不翻转为 down**。

### 议题 4：流式 fallback 边界再确认

**Dev**：§2.2 写了"首 token 前失败可无缝切候选，首 token 后中途错误沿用 M2 补发 error event，无法中途换 provider"。这个边界我认可，但要在 T-02 验收里明确写：**流式测试中，首 token 前主 provider 抛 quota → 必须切到备用并返回完整流**；首 token 后错误 → 补发 error event（不要求切换）。别让 QA 误以为流式要全程无缝切换。

**PM**：决议：**R4 — T-02 流式验收明确二分：首 token 前失败→切候选成功；首 token 后错误→补发 error event（边界，非 bug）**；非流式走完整 fallback 链。

### 议题 5：成本报表三维的默认视图

**QA**：US-M3-10a/b/c 三维度。控制台默认进哪个？建议 Dashboard 下方"近期异常"之外，用量报表页**默认按 Key 视图**（最受 admin 关心的"谁在烧"），模型/时间作为切换 tab。

**Dev**：可以，前端加 tab 切换 `group_by`，默认 `key`。后端 `/api/usage` 的 `group_by` 默认也设为 `key`。

**PM**：决议：**R5 — 用量报表默认 `group_by=key`（调用/tokens/花费/错误率），模型/时间切换 tab；后端 `group_by` 默认 `key`；时间范围默认近 7 天**；RBAC 越权 403 沿用 M2。

### 议题 6：Dashboard 指标卡数据口径

**Dev**：§2.8 四指标卡"今日调用量/花费/错误率/活跃 Key 数"。"活跃 Key 数"就是北极星 MAK 的当日版，OK。但"错误率"建议定义清楚 = 当日 `status != success` 的调用占比，避免和"配额误标记率"混。

**PM**：对，两个指标别混。决议：**R6 — Dashboard「错误率」= 当日 `status≠success` 调用占比；「配额误标记率」是独立健康指标（见 §5），不在四卡内，仅在「近期异常」列表体现**；四卡空状态引导"先创建虚拟 Key"。

### 议题 7：精确缓存命中范围

**QA**：§2.6 说"归一化去除无意义字段"。哪些算无意义？`temperature`/`top_p`/`seed` 肯定影响输出，必须进缓存键，否则不同参数命中同一缓存会答非所问。`stream` 字段本身不入键（缓存只服务非流式）。

**Dev**：同意，`temperature`/`top_p`/`seed`/`messages`/`model` 都进归一化键；`stream` 不入（且缓存只查非流式）。另外建议缓存键额外带 `llm_provider` 维度无需（model 已含 provider 前缀）。

**PM**：决议：**R7 — 精确缓存归一化键包含 `model + messages + temperature + top_p + seed` 等影响输出的参数；`stream` 不入键且缓存仅服务非流式；TTL 默认 1h 可配**。

### 议题 8：ARCHITECTURE 文档漂移回写

**PM**：v0.1 的 R-arch-9 已标出——ARCHITECTURE §4.4/§5 还写着 `quota_policy` JSON（`{"daily_tokens":...}`），但 M2 实际已实现成 `virtual_keys.daily_token_quota INT NULL` 列，§5 还残留 `expires_at`（M1 R6 已决定 VK 不含 expires_at）。今天顺手回写，免得 M3 实现时照着过时 schema 写。

**Dev**：对，还有 §4.5 的状态机图里 `quota_exhausted` 状态流转可以补一句"状态存 Redis 不落关系库"，和 R-arch-1 对齐。

**PM**：决议：**R8 — 回写 `ARCHITECTURE.md`：① §4.4/§5 `quota_policy` JSON → `daily_token_quota INT NULL` 列（与 M2 实现一致）；② §5 删 `virtual_keys.expires_at`（M1 R6 已定）；③ §4.5 状态机补注"provider 运行时状态存 Redis，键 `provider_status:{id}`，不落关系库"**。

---

## ✅ 最终决议（全员通过）

| 编号 | 决议 | 落点 |
|------|------|------|
| R1 | `classify_error` 以 `llm_provider`+`status_code`+原始 body/message 子串判定四类；quota 必须查 body/message（不依赖 `RateLimitErrorCategory`）；MVP 覆盖 OpenAI/DeepSeek/通义；T-01 去掉"spike 待定" | T-01 / §2.1 |
| R2 | 熔断阈值（失败率+窗口）做成全局可配项，默认 50%/30s；MVP 不暴露 per-provider 配置 UI | T-03 / §2.3 |
| R3 | 探活周期可配（默认 10min）；失败冷却指数退避（翻倍、上限 60min）；仅对 `quota_exhausted` 探活 | T-04 / §2.4 |
| R4 | 流式 fallback 验收二分：首 token 前失败→切候选；首 token 后错误→补发 error event（边界非 bug）；非流式走完整链 | T-02 |
| R5 | 用量报表默认 `group_by=key`，模型/时间切换 tab；时间范围默认近 7 天；后端默认 `key` | T-07 / §2.7 |
| R6 | Dashboard「错误率」= 当日 `status≠success` 占比；「配额误标记率」为独立健康指标不在四卡内 | T-08 / §2.8 |
| R7 | 精确缓存键含 `model+messages+temperature+top_p+seed`；`stream` 不入键且缓存仅非流式；TTL 默认 1h 可配 | T-06 / §2.6 |
| R8 | 回写 ARCHITECTURE：§4.4/§5 `quota_policy`→`daily_token_quota`；删 `expires_at`；§4.5 补"状态存 Redis 不落关系库" | ARCHITECTURE.md |

**PM**：M3 计划通过，回写 v0.2，ARCHITECTURE 漂移同步对齐。下一步按惯例——实现 M3。大家没异议吧？

**Dev / QA / 用户**：（一致）通过。
