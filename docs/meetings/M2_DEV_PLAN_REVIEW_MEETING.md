# M2 开发计划评审会纪要（模拟）

> 文档：`../technical/M2_DEV_PLAN.md` v0.1
> 日期：2026-07-24
> 参会：产品（PM）、开发（Dev）、测试（QA）、用户（实际使用者，团队里的开发者）
> 结论：**通过，回写 v0.2**（8 项决议 R1~R8）

---

## 🎬 会议实录

**PM**：M2 计划 v0.1 出来了，覆盖 US-M2-01~07 七条流式/额度/用量的故事。今天目标不是念文档，是把 M2 这种"第一次引入流式 + 真限额 + 真落库"最容易埋雷的点揪出来。Dev 你先开刀。

### 议题 1：客户端断开——只落日志够吗？

**Dev**：US-M2-03 写的是断开后 `stream.aclose()` + 写一条 `client_disconnect` 日志。但这里有个漏洞：日志记了，可 **Redis 的额度计数器没加 partial token**。客户端要是算准了——每次快用完就主动断开，上游已经烧了 token，但配额计数没动，他就能反复"断开 dodge 限额"。这是个真 bug。

**用户**：（点头）对，我断开不是为了省额度，但确实有人会利用。断开了也得把已烧的算进去。

**PM**：同意，这是硬伤。决议：**R1 — 客户端断开时，除落 `usage_logs` 外，必须同时 `INCRBY` Redis 配额计数器 partial tokens**，和正常结束走同一计数路径，杜绝 dodge。

### 议题 2：用量视图的越权参数

**QA**：US-M2-07 用量视图写"User 自见、Admin 可 `?scope=global` / `?vk_id=` 下钻"。但后端如果直接信任前端传的 `scope`/`account_id` 参数，User 自己拼个 `?scope=global` 就能看全公司的量。必须后端强制按角色覆盖参数。

**Dev**：对，参数该是"Admin 才允许的下钻维度"，非 admin 传了直接忽略或 403，绝不能拼进 SQL。

**PM**：决议：**R2 — `/api/usage` 的 `scope`/`global`/`account_id` 参数仅 Admin 有效；非 admin 一律按自身 `account_id` 强制过滤，传了越权参数返回 403 并记一条安全日志**。T-07 验收加一条"User 传 `?scope=global` 必须被拦"。

### 议题 3：mock echo 能不能测流式三连？

**Dev**：M1 的 `MOCK_PROVIDER=1` echo 现在只支持非流式。T-02（错误补发）要"发 3 块后抛错"、T-03（断开）要模拟客户端断开，T-01 要 stream 形态——**现在的 mock 一个都测不了**，得真 key 才能跑，CI 跑不了。

**QA**：我也要这个底座。建议给 mock echo 加两个开关：`?__error_after=N` 发 N 块后抛错、`?__stream=1` 走流式分块。这样 T-01/02/03 全能在无 key 环境端到端验。

**PM**：决议：**R3 — M1 的 mock echo 扩展支持 `?__stream=1` 分块返回 + `?__error_after=N` 注入中途错误**，T-08 的 pytest 全用它，不依赖真实 key。

### 议题 4：Redis 启动校验够不够？

**Dev**：T-04 写"校验 `REDIS_URL` 存在就 fail-loud"。但**有 URL 不等于能连**——Redis 没起、网络不通，启动时不报，第一个请求才挂。建议启动真正 `PING` 一次。

**PM**：合理。决议：**R4 — 启动对 Redis 做 `PING` 探活，连不上直接 fail-loud**；同时在日志里提示"计数仅存 Redis、重启会丢当日计数"这个已知限制，避免日后误判。

### 议题 5：额度"软硬"口径再确认

**Dev**：§2.4 的"请求粒度软硬限额"——判定在调用前，没法预知 completion token，所以单条大请求可能轻微超阈。这个我确认没问题，但想明确：超限判定用的 `current` 是**上一请求结束后的累计值**，不含当前进行中的请求。文档已写清，我认可。

**PM**：这是接受的行为，不纠结。决议：**R5 — 维持现状，仅把"计数基于历史累计、不含进行中请求"这句在 §2.4 加粗明示**，防误解。

### 议题 6：T 恤尺码复核

**Dev**：T-06 用量落库我之前标 M，但流式末块 `usage` 可能缺失、断开取 partial、`provider` 要 `split('/')[0]` 推导、cost 还要 `completion_cost` 估算——边界不少，实际偏 L。其余尺码我认可。

**PM**：升。**R6 — T-06 由 M 升 L**；其他 T-01 L / T-02 M / T-03 M / T-04 M / T-05 S / T-07 M / T-08 M 维持。

### 议题 7：SSE 格式要不要单测卡死？

**QA**：§5 风险写了"SSE 双换行必须精确，否则 SDK 不解析"。这不能只靠人工看，得有个单测断言原始响应字节里是 `data: ` 开头、`\n\n` 分隔、以 `data: [DONE]` 结尾。

**PM**：加。**R7 — T-08 加 pytest 断言 SSE 原始字节格式**（前缀/分隔/DONE），不放进人工验收。

### 议题 8：架构 §6.2 回写

**PM**：最后 R-arch-1 已标了——架构 §6.2 写的 `httpx.AsyncClient.stream()` 和 M1 定的 litellm 直连冲突。今天顺手回写掉，保持文档链一致。

**Dev**：对，不然 M3 的人看到 §6.2 又去引 httpx，白绕。

**PM**：决议：**R8 — 回写 `ARCHITECTURE.md` §6.2，把 `httpx.AsyncClient.stream()` 改为 LiteLLM `acompletion(stream=True)` 直连口径**，与 M1/M2 一致。

---

## ✅ 最终决议（全员通过）

| 编号 | 决议 | 落点 |
|------|------|------|
| R1 | 客户端断开须同时 `INCRBY` Redis 配额计数 partial tokens | T-03 + T-04 |
| R2 | 用量视图越权参数（`scope`/`global`/`account_id`）非 admin 强制拦 403 | T-07 |
| R3 | mock echo 扩展 `?__stream=1` + `?__error_after=N` | T-01 / T-08 |
| R4 | 启动对 Redis 做 `PING` 探活，连不上 fail-loud | T-04 |
| R5 | §2.4 加粗"计数基于历史累计、不含进行中请求" | §2.4 |
| R6 | T-06 尺度 M → L | §3 |
| R7 | T-08 加 SSE 原始字节格式 pytest | T-08 |
| R8 | 回写架构 §6.2 httpx→litellm 直连 | ARCHITECTURE.md |

**PM**：M2 计划通过，回写 v0.2，架构 §6.2 同步。下一步按惯例——要么开 M3 计划，要么先实现 M2。大家没异议吧？

**Dev / QA / 用户**：（一致）通过。
