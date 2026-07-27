# M1 开发计划评审会议纪要（模拟）

> 文档：`../technical/M1_DEV_PLAN.md` v0.1
> 日期：2026-07-24
> 参会：产品（PM）、开发（Dev）、测试（QA）、用户（User，团队内开发者）
> 目的：评审 M1 开发计划（US-M1-00~14，15 条故事），揪粒度 / 遗漏 / 技术风险，决议回写 v0.2。

---

## 🎬 评审过程

**PM**：M1 计划写完了，覆盖账号/RBAC/VK/多 Provider/路由别名/控制台基础页。今天重点不是念文档，是把"第一次引入账号、会话、SQLite"这几个新东西的雷摆出来。Dev 你先开炮。

### 议题 1：登出与会话——"禁用账号即时生效"对得上吗？

**Dev**：有个矛盾我必须先说。§2.3 写的是"短过期 JWT + 前端清 cookie，登出即失效"，默认 `SESSION_EXPIRE_MIN=60`。但 US-M1-03 的 AC 是"禁用账号即时生效"。这是个无状态 JWT——账号被禁用后，他手里那个合法签发的 JWT 在 60 分钟内照样能调 admin API，直到过期。**"即时生效"根本没达成**。

**QA**：对，这正是我的越权回归点。禁用 admin 后，他旧 token 还能用一小时，等于禁用形同虚设。US-M1-03 写的是即时，文档实现却给了一小时窗口。

**PM**：我同意这是真矛盾。但我们是小团队，要不要在 M1 就上 Redis 会话存储？前面 M0 特意把 Redis 拿掉了。

**Dev**：不用 Redis。我们已经引入 SQLite 了，直接在 DB 里建一张 `sessions` 表存会话令牌（带 expiry + revoked 标记），每次请求验库。登出就 `revoked=true`；禁用账号就把它名下所有 session `revoked=true`。零额外组件，且"禁用即时生效"真正成立。

**User**：这个我站 Dev。账号被禁用还留一小时后门，我接受不了。

**PM**：决议——**放弃无状态 JWT 注销，改 DB-backed 会话**：cookie 存随机 session token（httpOnly+SameSite），`sessions` 表验库 + revoked 标记。登出/禁用即时失效。US-M1-03 的"即时生效"才站得住。

### 议题 2：依赖——别在 3.13 再踩一遍坑

**Dev**：第二个雷，而且是刚踩过的类型。§2.9 列了 `passlib[bcrypt]`。bcrypt 有 C 扩展，在 Python 3.13 上装包经常编译失败——咱们刚被 litellm 的 `imghdr` 坑过一轮，没必要再引一个 3.13 构建风险。

**QA**：同意，少一个原生依赖少一份 CI 崩的概率。

**Dev**：口令哈希我用标准库 `hashlib.pbkdf2_hmac` + `secrets` 随机盐，零依赖、够安全。JWT 库 `pyjwt` 是纯 Python，没问题，可以留。

**PM**：那 `passlib[bcrypt]` 划掉，改 stdlib。还有别的原生依赖吗？

**Dev**：`pyjwt` 纯 Python，安全。`sqlalchemy`/`aiosqlite` 都纯 Python，OK。所以 M1 新增依赖全是纯 Python，无构建风险。

**PM**：决议——**去掉 passlib，口令哈希用 `hashlib.pbkdf2_hmac`；保留 `pyjwt`（纯 Python）**。这是从 M0 litellm 教训直接推出来的铁律：M1+ 新增依赖优先纯 Python。

### 议题 3：路由别名 schema 口径冲突

**Dev**：第三个，文档自己埋了个不一致。§2.1 把 `model_routes.providers` 解释成"有序的 LiteLLM 模型串列表"，但 ARCHITECTURE §5 的注释写的是"provider ids"。两处对同一个字段定义不同，实现时以哪个为准？

**PM**：这是我在写 M1 计划时主动具体化、并标了 ⚠️ 待回写的点。我的倾向是**用模型串列表**——LiteLLM 调用本就吃 `openai/gpt-4o-mini` 这种形式，直接存模型串零转换，路由时再 `split('/')[0]` 切出 provider 前缀去查 `enabled` 和健康。比"存 provider id 再拼 model"少一层映射。

**QA**：只要定死一种，我用例就好写。建议明确：存完整模型串，`providers` 表不需要 `default_model` 列，前缀现切。

**PM**：决议——**`model_routes.providers` = 有序 LiteLLM 模型串列表**（如 `["openai/gpt-4o-mini","deepseek/deepseek-chat"]`）；provider 前缀 `split('/')[0]` 现切，不新增 `default_model` 列。**M1 评审后回写 ARCHITECTURE §5 注释对齐**，消除不一致。

### 议题 4：别名路由怎么测——没有真 key 怎么办

**QA**：T-09 要端到端验"用 VK 以别名调 `/v1` 命中候选"。但 CI 和我们本地都没有真厂商 key（M0 也是这么跳过的）。别名路由不验等于没写。

**Dev**：加一个**测试专用 echo 适配器**：env 置 `MOCK_PROVIDER=1` 时，`adapters.completion` 对特定测试模型（如 `mock/echo`）直接返回固定响应，不真调 LiteLLM。仅测试用，生产不走。T-09 用这个模型串建别名、验命中。

**User**：好，这样别名路由切换逻辑能被真实验，而不是靠我手点。

**PM**：决议——**新增测试专用 echo/mock 适配路径（`MOCK_PROVIDER` 开关），仅 T-09/集成测试用**，生产默认关闭。

### 议题 5：初始管理员引导的安全默认值

**Dev**：US-M1-00 用 `BOOTSTRAP_ADMIN_PASSWORD` 建首个 admin。如果运维忘了配这个 env 怎么办？文档没说。要是"没配就自动生成个随机密码打印出来"，那就是不安全的默认。

**PM**：应该 fail loud——账号表空且没给 `BOOTSTRAP_ADMIN_PASSWORD`，启动直接报错退出，逼着配。绝不悄悄给个弱默认。

**User**：嗯，宁可起不来，也别起一个谁都能进的后台。

**PM**：决议——**`accounts` 空且缺 `BOOTSTRAP_ADMIN_PASSWORD` → 启动失败（fail loud）**，不生成弱默认。

### 议题 6：VK 要不要"过期时间"

**Dev**：§2.2 提到 VK 校验 `expires_at`，但 US-M1-06 的故事里压根没提过期。M1 故事没有"Key 定期失效"这条需求，加了是超范围。

**PM**：同意，过期不在 M1 范围。`expires_at` 划掉，M1 的 VK 只有"启用/禁用"两种状态，不过期。需要过期以后再议。

**PM**：决议——**M1 VK 不含 `expires_at`，仅 `enabled` 状态**；过期能力后置，避免范围蔓延。

### 议题 7：auth_ref 与 env 变量名的映射

**QA**：§2.5 的 `auth_ref` 到底怎么对应 `.env` 里的 `OPENAI_API_KEY`？测试我要断言"控制台只存了引用名、没存真 key"。

**Dev**：约定：`auth_ref` 直接存 provider 前缀（如 `openai`/`deepseek`/`qwen`），LiteLLM 自动读对应 `OPENAI_API_KEY` 等 env。控制台永远只碰 `auth_ref` 字符串，真 key 仅存在于进程 env。这样映射零配置、最直观。

**PM**：决议——**`auth_ref` = provider 前缀（如 `openai`），与 LiteLLM env 约定一致**，文档补这个约定。

---

## ✅ 最终决议（全员通过）

| # | 决议 | 影响 |
|---|------|------|
| R1 | 放弃无状态 JWT 注销，改 **DB-backed 会话**（`sessions` 表 + revoked 标记），禁用账号/登出即时失效 | 新增 `sessions` 表；US-M1-03"即时生效"成立 |
| R2 | 去掉 `passlib[bcrypt]`，口令哈希用 **`hashlib.pbkdf2_hmac` + secrets 盐**；保留纯 Python 的 `pyjwt` | M1 新增依赖全纯 Python，无构建风险 |
| R3 | `model_routes.providers` = **有序 LiteLLM 模型串列表**；前缀 `split('/')[0]` 现切；回写 ARCHITECTURE §5 对齐 | 消除文档间 schema 不一致 |
| R4 | 新增 **测试专用 echo 适配器**（`MOCK_PROVIDER` 开关），T-09 端到端验别名路由 | 无真 key 也能验路由切换 |
| R5 | `accounts` 空且缺 `BOOTSTRAP_ADMIN_PASSWORD` → **启动失败（fail loud）** | 杜绝弱默认 admin |
| R6 | M1 VK **不含 `expires_at`**，仅 `enabled` 状态 | 防范围蔓延 |
| R7 | `auth_ref` = provider 前缀（如 `openai`），对应 LiteLLM env 约定 | 映射零配置、可测 |

**PM**：总结——M1 计划通过，回写 v0.2，并把 R3 同步回写 ARCHITECTURE §5。七项修订里 R1（会话态）和 R2（依赖去原生）是这次最关键的两个，直接决定 M1 的安全和能否顺利装包。大家没异议？

**Dev / QA / User**：（一致）通过。

---

## 行动项

- [ ] 回写 `M1_DEV_PLAN.md` → v0.2（落实 R1~R7）
- [ ] 回写 `ARCHITECTURE.md` §5 注释：`providers` 字段口径对齐"模型串列表"，消除与 M1 计划的出入
- [ ] T-04 任务补充：禁用账号须级联 `revoked` 所有 session（R1）
- [ ] 新增依赖清单更新：`passlib` 删除，`pyjwt` 保留；补 `sessions` 表到数据模型
