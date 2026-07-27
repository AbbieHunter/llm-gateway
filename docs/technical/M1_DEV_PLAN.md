# M1 开发任务拆解与技术文档（Dev Plan）

> 版本：v0.2
> 日期：2026-07-24
> 范围：M1 —— 账号体系 + RBAC + 虚拟 Key 鉴权 + 多 Provider 适配 + 路由别名（failover/weighted + 健康过滤）+ 控制台基础页
> 对应故事：`../product/USER_STORIES.md` US-M1-00 ~ US-M1-14（共 15 条）
> 关联：`ARCHITECTURE.md` v0.2 §4.3（鉴权/RBAC）、§4.5（路由）、§5（数据模型）、§7（目录结构）
> 定位：本文档是 M1 的**任务拆解 + 技术规格**，不含代码实现。M2+ 将单独成篇。
> v0.2 修订：评审会决议 R1~R7 已回写（DB-backed 会话、stdlib 口令哈希、路由别名 schema 定稿、测试 mock 适配器、引导 fail-loud、VK 不过期、auth_ref 约定）。

---

## 1. M1 范围与目标

**目标一句话**：把"谁能进控制台、谁能拿 Key、用哪个模型"这三件事真正管起来——账号+RBAC 落地、虚拟 Key 鉴权生效、多 provider 经别名路由可切换、控制台能实际建 Key/配 Provider/配路由。

M1 是**功能闭环的关键里程碑**：M0 只验证了"能调通"，M1 才让网关具备"可分发、可管理、可切换"的雏形。

| 故事 | 目标 | 不在 M1 的事（防超范围） |
|------|------|--------------------------|
| US-M1-00 初始管理员引导 | 无账号时由 env 建首个 admin，打破"无人能登录"死锁 | 不做公开注册页 |
| US-M1-01 账号登录 | 用户名+口令登录拿会话 | 不做 MFA、不做自助改密（Won't 清单） |
| US-M1-02 创建账号/角色 | admin 建账号并分 admin/user | 不做操作审计日志（Won't） |
| US-M1-03 禁用/改角色 | admin 禁用账号或改角色**即时生效**（会话级联失效） | — |
| US-M1-04 普通用户受限视图 | user 仅见自身 Key/用量，导航隐藏管理菜单 | 菜单隐藏仅 UX，**非安全边界** |
| US-M1-05 后端 RBAC 强制 | 所有 `/api/*` 按角色+`owner_account_id` 过滤，越权拒绝 | **前端隐藏 ≠ 安全**，后端为唯一真相源 |
| US-M1-06 创建虚拟 Key | 建 VK、绑每日额度、明文仅返一次、哈希入库 | 额度**扣减**在 M2，M1 仅存策略；**VK 不过期**（无 `expires_at`） |
| US-M1-07 Key 列表/掩码 | 列表掩码、状态徽标 | 今日已用/剩余数值 M2 才有 |
| US-M1-08 Key 启用/禁用/删除 | 禁用即拒调用、删除二次确认 | — |
| US-M1-09 重置 Key | 旧失效、新明文仅返一次，用量归属不变 | — |
| US-M1-10 配置 Provider | 控制台填 `auth_ref`（provider 前缀）、启停；存 DB | **真 key 不落库**；新增需重启(架构§4.2) |
| US-M1-11 创建路由别名 | 别名→候选模型串有序列表 + failover/weighted | cost 策略 Phase 2 |
| US-M1-12 按别名路由 | `model=别名` 时按策略路由到候选 | 配额感知标记/重试/熔断在 M3 |
| US-M1-13 跳过不可用 Provider | 路由跳过 `enabled=false` / Redis 状态非 healthy | 自动标记 unhealthy 在 M3 |
| US-M1-14 登出 | 主动登出使会话即时失效 | — |

**M1 完成判据（DoD）**：用 admin 账号登录后可建账号、建 VK（明文仅一次）、配 Provider、建别名；用 VK（经 `base_url`）以别名调用能按 failover/weighted 命中候选 provider；user 账号登录后看不到管理菜单且**直调 admin API 被 403**；禁用某 admin/user 后其**现有会话立即失效**；控制台基础页（Keys/Providers/Routes/Accounts/登录/Dashboard 占位）可操作。

---

## 2. 技术设计（M1）

### 2.1 数据模型与持久化

引入 **SQLite + SQLAlchemy（async）**，新建 `app/db/`：

- `app/db/session.py`：`DATABASE_URL`（`env`，默认 `sqlite+aiosqlite:///./data/gateway.db`），`async_session_factory`、依赖 `get_db`。
- `app/db/models.py`：Account / VirtualKey / Provider / ModelRoute / **Session（R1 新增）**。字段对齐 `ARCHITECTURE.md` §5（§5 注释已在本次评审回写为"模型串列表"口径）。
- `app/db/seed.py`：启动若 `providers` 表空，写入种子 provider（openai / deepseek / qwen，仅 `auth_ref` + `enabled` + `weight`，**不含真 key**）。

**M1 字段口径（已定稿，消除与架构 §5 的不一致，R3）**：
- `model_routes.providers`：**有序的 LiteLLM 模型串列表**（如 `["openai/gpt-4o-mini","deepseek/deepseek-chat"]`）。理由：LiteLLM 调用本就吃 `"provider/model"` 形式，直接存模型串零转换；路由时 `split('/')[0]` 现切 provider 前缀查 `providers.enabled` 与 Redis 健康状态。**不新增 `default_model` 列**。
- `virtual_keys`：沿用 `key_hash`(SHA-256)、`quota_policy`(JSON `{"daily_tokens":int|null}`)，M1 仅存储策略，**扣减在 M2**。**不含 `expires_at`**（VK 不过期，R6）。
- `accounts`：`status`(`active|disabled`)、`role`(`admin|user`)、`password_hash`（stdlib pbkdf2，R2）。
- `sessions`（R1 新增）：`token_jti`(str, PK)、`account_id`(FK)、`expires_at`、`revoked`(bool, 默认 False)、`created_at`。用于"禁用账号/登出即时失效"。

### 2.2 双鉴权体系（核心）

沿用架构 §4.3 的"两套鉴权，职责分明"，M1 落地：

**(a) 网关 API `/v1/*` — 虚拟 Key 鉴权（新建 `app/middleware/vk_auth.py`）**
- 解析 `Authorization: Bearer sk-...` → SHA-256 哈希 → 查 `virtual_keys`。
- 校验：存在、`status=active`；命中后把 `account_id` / `quota_policy` / `vk_id` 注入 `request.state`。
- 失败 → `401` + OpenAI 错误 JSON。**M1 仅鉴权，不限流**（限流 M2）。

**(b) 控制台 API `/api/*` — 账号会话鉴权 + RBAC（新建 `app/middleware/session_auth.py` + `app/core/security.py`）**

> **R1（关键变更）**：会话改为 **DB-backed**，不再是"无状态短过期 JWT"。原因：US-M1-03 要求"禁用账号即时生效"，无状态 JWT 在有效期内仍可用，矛盾。

- `security.py`：
  - `hash_password` / `verify_password`：**stdlib `hashlib.pbkdf2_hmac` + `secrets` 随机盐**（R2，零依赖，避开 3.13 原生包构建坑）。
  - `create_session(account_id) -> token`：签发 **pyjwt**（纯 Python）签名 token，payload=`{sub:account_id, jti, exp}`；并把 `jti` 写入 `sessions` 表（`revoked=False`）。token 置于 **httpOnly + SameSite=Lax** cookie（防 XSS 读 token）。
  - `revoke_session(jti)` / `revoke_all_for_account(account_id)`：置 `sessions.revoked=True`（登出 / 禁用账号级联用）。
- `session_auth` 依赖：读 cookie → 验 JWT 签名+过期 → 取 `jti` → 查 `sessions` 表 → **`revoked` 必须为 False** → 加载 Account → 注入 `request.state.account`。无效/过期/已吊销 → `401`。
- **RBAC 依赖 `require_admin`**：非 admin 访问 admin-only 端点 → `403`。
- **所有权过滤 helper `filter_by_owner`**：user 查 VK 时强制 `WHERE owner_account_id = :me`；admin 不加过滤。**这是越权防护的真相源（US-M1-05）**。

### 2.3 账号与 RBAC API

`app/routers/console.py` 中账号相关端点：

| 端点 | 方法 | 权限 | 说明 |
|------|------|------|------|
| `/api/auth/login` | POST | 公开 | `{username,password}` → 校验 → `create_session` + Set-Cookie；错误口令明确 401 |
| `/api/auth/logout` | POST | 登录态 | `revoke_session(当前 jti)` → 清 cookie（**即时失效，R1**） |
| `/api/accounts` | GET | admin | 账号列表（username/role/status/created_at） |
| `/api/accounts` | POST | admin | 新建账号 `{username,password,role}`；口令哈希入库 |
| `/api/accounts/{id}` | PATCH | admin | 禁用/启用（`status`）、改角色（`role`）；**禁用须 `revoke_all_for_account` 级联失效其会话（R1，US-M1-03）** |
| `/api/me` | GET | 登录态 | 返回当前账号信息（前端判断角色/导航） |

- **越权用例（QA 必覆盖，US-M1-05）**：user 直调 `GET /api/accounts`、直调他人 VK 接口 → `403` 或空结果。
- **禁用即时生效（R1）**：禁用账号 → 其所有 `sessions` 行 `revoked=True` → 旧 token 下次请求即 `401`。

### 2.4 虚拟 Key API

| 端点 | 方法 | 权限 | 说明 |
|------|------|------|------|
| `/api/keys` | GET | admin=全部 / user=自身 | 列表，Key 掩码（`sk-****1234`）、状态、额度、owner |
| `/api/keys` | POST | admin | 建 VK：`{name, owner_account_id, daily_tokens?, enabled?}`；生成随机 32 字节 hex → 入库存 SHA-256 → **明文仅响应一次** |
| `/api/keys/{id}` | GET | 所有者/admin | 掩码详情 |
| `/api/keys/{id}` | PATCH | admin | 启用/禁用 |
| `/api/keys/{id}` | DELETE | admin | 删除（前端二次确认 + 影响范围提示） |
| `/api/keys/{id}/reset` | POST | admin | 旧失效、生成新明文（仅返一次）；`vk_id` 不变 → 历史用量归属不变 |

- 明文展示页要点（对齐产品设计）：创建成功页展示明文 + 复制按钮 + 警告"仅显示一次"。

### 2.5 Provider 配置 API

| 端点 | 方法 | 权限 | 说明 |
|------|------|------|------|
| `/api/providers` | GET | admin | 列表（id/`auth_ref`/display_name/enabled/weight） |
| `/api/providers` | POST | admin | 新建：`{id, display_name, auth_ref, weight?, enabled?}`；**`auth_ref` = provider 前缀（R7，如 `openai`），与 LiteLLM env 约定一致，不填真 key** |
| `/api/providers/{id}` | PATCH | admin | 启停、改权重 |

- **`auth_ref` 约定（R7）**：直接存 provider 前缀（`openai`/`deepseek`/`qwen`），LiteLLM 自动读对应 `OPENAI_API_KEY` 等 env。控制台永远只碰 `auth_ref` 字符串，**真 key 仅存在于进程 env**，不落库。
- **重启摩擦（架构 §4.2）**：真 key 走 env，M1 不改变"改 env 需重启"；控制台仅管 `auth_ref` 名与启停。响应/文档须提示"新增 provider 凭证需在 `.env` 加 `XXX_API_KEY` 并重启网关生效"。
- 健康状态（Redis）不在此 CRUD，见 §2.8。

### 2.6 路由别名 API + 路由解析接入

**别名 CRUD（admin）**：

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/routes` | GET | 别名列表（alias/providers 有序模型串/strategy） |
| `/api/routes` | POST | 新建：`{alias, providers:[litellm 模型串...], strategy}` |
| `/api/routes/{alias}` | PATCH | 改候选顺序/策略/启停 |
| `/api/routes/{alias}` | DELETE | 删别名 |

**路由解析接入 `app/core/router.py`（M1 新增，M3 扩）**：
- `resolve(target_model) -> list[candidate]`：
  - 若 `target_model` 是别名（命中 `model_routes`）→ 取有序 `providers` 模型串列表；
  - 否则（`target_model` 是具体模型）→ 透传，候选=[target_model]。
- **过滤不可用**：遍历候选，用 `/` 切出 provider 前缀 → 查 `providers.enabled`（`False` 跳过）+ `health.get_status(prefix)`（`!= healthy` 跳过，见 §2.8）。
- **策略排序**：
  - `failover`：保持列表顺序（首项首选）；
  - `weighted`：按 `providers.weight` 做加权轮询（M1 用简单轮询/随机加权）。
- **调用与 fallback（M1 简化版）**：`adapters.completion(model=candidate, ...)`，失败（任意异常）按顺序试下一候选；全失败 → 返回 502 + 各候选失败原因（**错误明细**，架构 §6.3，让开发者分清网关侧问题）。
  > ⚠️ M1 的 fallback 是"顺序试下一候选"的**朴素版**；M3 才做错误 code 分类（QUOTA_EXHAUSTED 不重试、RATE_LIMITED/5xx 退避重试、熔断）。M1 路由已预留 `errors.py` 接缝，M3 仅往里填逻辑，不返工。
- `/v1/models`（改造）：返回静态基础模型 + DB 中别名（`id=alias, owned_by=llm-gateway`）。

### 2.7 控制台基础前端（React）

在 M0 的 Vite 空壳上**长出真实页面**（不做精雕样式，但功能可操作）：

- **`frontend/src/auth/`**：登录页（用户名+口令）、登录态管理（读 `/api/me`）、登出。
- **`frontend/src/components/AppShell.tsx`**：顶部栏（用户信息 + 登出按钮）+ 左侧导航。**导航按角色隐藏**（user 不显示 Accounts/Providers/Routes，US-M1-04）。
- **`frontend/src/pages/`**：
  - `Login.tsx`
  - `Dashboard.tsx`（**占位**：标题 + "先创建虚拟 Key"空状态引导，真实指标 M3）
  - `Keys.tsx`（建 Key 表单 + 列表掩码 + 启用/禁用/删除/重置；创建成功弹窗展示明文+复制+警告）
  - `Providers.tsx`（列表 + 新建/启停，仅填 `auth_ref` 前缀名）
  - `Routes.tsx`（别名 CRUD + 候选顺序 + 策略选择）
  - `Accounts.tsx`（admin 专属：列表 + 新建 + 禁用/改角色）
- **契约**：所有数据走 `/api/*` 带 cookie；破坏性操作二次确认；敏感信息（VK 明文仅一次、`auth_ref` 仅前缀名）遵守架构 §4.9。
- **不做**：移动端、图表、审计日志（Won't）。

### 2.8 健康过滤（M1 轻量版）

- 新建 `app/core/health.py`：`get_status(provider_id) -> str`，读取 Redis key `provider:{id}:status`。
- **M1 不强制引入 Redis**（与 M0 去 Redis 一致）：若 `REDIS_URL` 未配置，`get_status` 一律返回 `healthy`；配置后读取真实状态。M3 的探活/标记写入同一 Redis 键，M1 代码无需改。
- 路由过滤：`enabled=False`（DB）或 `status != healthy`（Redis）的候选被跳过。M3 会自动把 `quota_exhausted`/`degraded` 写进该键，M1 的过滤逻辑天然继承。

### 2.9 配置与依赖

- **新增依赖（写入 `requirements.txt`，全纯 Python，R2）**：
  - `sqlalchemy>=2.0`（ORM，async）
  - `aiosqlite`（async sqlite 驱动）
  - `pyjwt`（会话 token 签名，纯 Python）
  - ~~`passlib[bcrypt]`~~ **已删除**：口令哈希改 **stdlib `hashlib.pbkdf2_hmac` + `secrets`**，零依赖、无 3.13 构建风险。
- **新增环境变量**：`DATABASE_URL`（默认 sqlite 文件）、`BOOTSTRAP_ADMIN_USERNAME`（默认 `admin`）、`BOOTSTRAP_ADMIN_PASSWORD`（**必填**，缺则启动失败，R5）、`JWT_SECRET`、`REDIS_URL`（可选，M1 可不配）、`SESSION_EXPIRE_MIN`（默认 60，仅作用于 DB 会话 `expires_at`）、`MOCK_PROVIDER`（默认 0，测试用，R4）。
- **`.gitignore`**：追加 `data/`（sqlite 文件，运行时生成）。

### 2.10 初始管理员引导（fail-loud，R5）

- 启动时若 `accounts` 表空：
  - 有 `BOOTSTRAP_ADMIN_PASSWORD` → 用其建 admin（用户名默认 `admin`），口令经 stdlib 哈希入库；
  - **无 `BOOTSTRAP_ADMIN_PASSWORD` → 启动直接失败（抛异常退出），绝不生成弱默认口令**（R5）。
- 已有账号则不触发；不暴露注册页。

### 2.11 测试专用 Mock 适配器（R4）

- `app/core/adapters.py`：当 `MOCK_PROVIDER=1` 且 `model == "mock/echo"` 时，`completion` 直接返回固定 OpenAI 兼容响应（含 `usage`），**不真调 LiteLLM**。
- 仅 T-09 集成测试使用；生产 `MOCK_PROVIDER` 默认 0，走真实 LiteLLM。
- 用途：无真厂商 key 也能端到端验证「VK 鉴权 → 别名路由 → 命中候选」链路。

### 2.12 验收映射

| 故事 AC | M1 设计落点 |
|---------|-------------|
| US-M1-00：无账号时 env 建 admin（缺 env 则 fail-loud） | §2.10（T-03） |
| US-M1-01/14：登录/登出、会话**即时**失效 | §2.2/§2.3（T-02/T-03） |
| US-M1-02/03：账号 CRUD/角色/禁用（**禁用即时失效会话**） | §2.3（T-04） |
| US-M1-04/05：user 受限视图 + 后端越权拒绝 | §2.2 RBAC + 所有权过滤（T-04，QA 重点） |
| US-M1-06~09：VK 全生命周期（不过期） | §2.4（T-05） |
| US-M1-10：Provider 配置（`auth_ref` 前缀） | §2.5（T-06） |
| US-M1-11~13：别名 + 路由 + 跳过不可用 | §2.6/§2.8（T-07） |

---

## 3. 开发任务拆解（Task Breakdown）

> T 恤尺码（S/M/L）为 **M1 建议值，评审会已确认**（R1~R7 已落实）。每个任务含可勾选子项与对应故事/AC。

### T-01 · 数据模型与持久化 — 【M】
- 对应：US-M1-00 / US-M1-06 / US-M1-10 / US-M1-11（基础设施）
- [ ] `app/db/session.py`：`DATABASE_URL` + async session + `get_db` 依赖
- [ ] `app/db/models.py`：Account / VirtualKey / Provider / ModelRoute / **Session**（字段见 §2.1）
- [ ] 启动建表（SQLAlchemy `create_all` 或轻量迁移）
- [ ] `app/db/seed.py`：providers 空时种子 openai/deepseek/qwen（仅 `auth_ref`+enabled+weight）
- 验收：`python -c` 能建表并 seed；account/vk/provider/route/session 可 CRUD

### T-02 · 口令哈希与会话机制 — 【M】
- 对应：US-M1-01 / US-M1-14（会话）
- [ ] `app/core/security.py`：`hash_password`/`verify_password`（**stdlib pbkdf2_hmac**，R2）
- [ ] `create_session`/`decode_session`（**pyjwt 签名 + `sessions` 表 jti 校验**，R1）
- [ ] `revoke_session` / `revoke_all_for_account`（置 `revoked=True`）
- [ ] 登录/登出端点骨架（具体路由在 T-04 串）
- 验收：口令不落明文；token 可签发/校验/过期；revoke 后失效

### T-03 · 初始管理员引导（fail-loud） — 【S】
- 对应：US-M1-00
- [ ] 启动时若 `accounts` 表空且有 `BOOTSTRAP_ADMIN_PASSWORD` → 建 admin
- [ ] **无 `BOOTSTRAP_ADMIN_PASSWORD` → 启动失败**（R5）
- [ ] 已有账号不触发；不暴露注册页
- 验收：配 env 首启可登录；二次启动不重建；缺 env 首启直接报错退出

### T-04 · 账号 CRUD + 后端 RBAC 中间件 — 【L】
- 对应：US-M1-02 / US-M1-03 / US-M1-05（**测试重点**）
- [ ] `app/routers/console.py`：login/logout/`/api/accounts` CRUD/`/api/me`
- [ ] `app/middleware/session_auth.py`：校验会话（含 `sessions.revoked` 查库）+ 注入 account
- [ ] `require_admin` 依赖（非 admin→403）
- [ ] `filter_by_owner` helper（user 仅自身 VK 查询）
- [ ] **禁用账号：级联 `revoke_all_for_account`（R1，US-M1-03 即时生效）**
- 验收：admin 可建/禁/改角色；**user 直调 `/api/accounts` → 403**（QA 越权用例）；禁用后该用户旧会话下次请求即 401

### T-05 · 虚拟 Key API — 【M】
- 对应：US-M1-06 / US-M1-07 / US-M1-08 / US-M1-09
- [ ] `POST /api/keys`：生成 32B hex → SHA-256 入库 → 明文仅返一次（无 `expires_at`，R6）
- [ ] `GET /api/keys`：admin 全部 / user 自身，掩码 + 状态
- [ ] PATCH 启用/禁用、DELETE（逻辑删，二次确认前端）、POST reset（旧失效、新明文一次）
- 验收：建 Key 返回明文一次；掩码列表；禁用后 `/v1` 调用该 VK 被拒；重置后旧失效

### T-06 · Provider 配置 API — 【M】
- 对应：US-M1-10
- [ ] `GET/POST/PATCH /api/providers`：`auth_ref`(前缀名) + 启停 + 权重（R7）
- [ ] 响应/文档提示"新增凭证需改 `.env` + 重启"
- 验收：列表/新建/启停可用；真 key 不落库（仅 `auth_ref`）

### T-07 · 路由别名 API + 路由解析接入 — 【L】
- 对应：US-M1-11 / US-M1-12 / US-M1-13
- [ ] `GET/POST/PATCH/DELETE /api/routes`：别名 + 有序模型串 + failover/weighted
- [ ] `app/core/router.py`：`resolve` 别名→候选 + 过滤 `enabled=False`/状态非 healthy（§2.8）
- [ ] `app/core/health.py`：`get_status`（Redis 可选，未配返回 healthy）
- [ ] 改造 `/v1/chat/completions`：接 VK 鉴权 → `resolve(model)` → failover/weighted 试候选 → 全失败 502 明细
- [ ] 改造 `/v1/models`：返回基础模型 + 别名
- 验收：建别名 `fast-chat`→[openai,deepseek]；用 VK 以 `model=fast-chat` 调用按序命中；禁用某 provider 后被跳过；全不可用返 502 明细

### T-08 · 控制台基础前端 — 【L】
- 对应：US-M1-04 / US-M1-01（UI）/ US-M0-02（页面化）
- [ ] 登录页 + 登录态（`/api/me`）
- [ ] AppShell：顶部栏（用户+登出）+ 左侧导航（按角色隐藏 admin 菜单）
- [ ] Keys / Providers / Routes / Accounts(admin) / Dashboard(占位) 页面（功能可操作，轻样式）
- [ ] VK 创建成功弹窗（明文+复制+警告）；破坏性操作二次确认
- 验收：admin 全流程可操作；user 登录后无管理菜单且功能不可达

### T-09 · 冒烟/集成脚本（M1 端到端） — 【M】
- 对应：US-M1-00~14（验收自动化）
- [ ] 扩展 `scripts/smoke.sh` / 新增 `scripts/smoke_m1.py`：`MOCK_PROVIDER=1` 起服务 → 引导建 admin → 登录拿 cookie → 建 user → **user 调 `/api/accounts` 断言 403** → 建 VK → 建别名(含 `mock/echo`) → 用 VK 以别名调 `/v1` 断言命中 mock 候选（R4）
- [ ] 文档标注可接入 CI
- 验收：脚本全绿；覆盖越权与别名路由两条主链路

### 依赖关系
```
T-01 (DB/模型) ──▶ T-03(引导), T-04(账号), T-05(VK), T-06(Provider), T-07(路由)
T-02 (哈希/会话) ──▶ T-03, T-04, T-08(登录UI)
T-04 (RBAC) ──▶ T-05(VK 所有权), T-09
T-07 (路由解析) ──▶ T-09
T-05,T-06,T-07 ──▶ T-08 (前端依赖各数据 API)
T-01,T-02,T-04 完成 → T-08 可并行开发
```
> T-03/T-04 是 M1 最早收口项（RBAC 是其它管理功能前置，评审会定排第一）。T-08 前端可与后端 API 并行（先 mock 契约）。

---

## 4. M1 完成定义（Definition of Done）

- [ ] 首次启动用 `BOOTSTRAP_ADMIN_PASSWORD` 建 admin 并可登录；**缺该 env 时启动失败**；二次启动不重建
- [ ] 登录发会话（httpOnly cookie + DB `sessions` 行）；登出即 `revoked` 失效；错误口令 401
- [ ] admin 可建/禁/改角色账号；**禁用账号后其所有现有会话立即失效**（R1）
- [ ] 口令哈希入库不落明文（stdlib pbkdf2）
- [ ] **后端 RBAC**：user 直调 admin API → 403；user VK 查询仅自身（QA 越权用例通过）
- [ ] 虚拟 Key：创建返明文一次、入库仅哈希；掩码列表；启用/禁用/删除/重置可用；禁用后 `/v1` 拒；**VK 无过期**
- [ ] Provider：控制台仅管 `auth_ref`(前缀名)+启停，真 key 不落库；提示重启摩擦
- [ ] 路由别名：failover/weighted 可配；`model=别名` 按策略命中候选；跳过 `enabled=False`/非 healthy；全不可用返 502 明细
- [ ] `/v1/models` 返回基础模型 + 别名
- [ ] 控制台基础页（登录/Keys/Providers/Routes/Accounts/Dashboard 占位）admin 全流程可操作；user 无管理菜单且功能不可达
- [ ] `scripts/smoke_m1.py` 全绿（含越权 + 别名路由主链路，R4 mock 适配器）
- [ ] 新增依赖写入 `requirements.txt`（全纯 Python）；`data/` 入 `.gitignore`；`.env.example` 补 M1 变量

---

## 5. 风险与注意

- **RBAC 形同虚设（最高优先）**：若只前端隐藏菜单而不后端过滤，user 可越权。**后端为唯一真相源**（US-M1-05），`filter_by_owner` + `require_admin` 必须兜底；QA 越权用例必覆盖（架构 §9.2）。
- **会话吊销必须落地（R1）**：禁用账号/登出不只是"前端清 cookie"，必须 `sessions.revoked=True` 并在 `session_auth` 查库校验，否则"禁用即时生效"不成立。
- **VK 明文泄露**：明文仅创建/重置响应一次，绝入库、绝写日志；前端创建成功页即销毁本地副本（仅复制动作）。
- **依赖零原生扩展（R2，M0 教训）**：M1+ 新增依赖优先纯 Python。`passlib[bcrypt]` 已删，口令哈希用 stdlib；`pyjwt` 纯 Python 可留。CI 不再因 bcrypt/C 扩展在 3.13 上编译失败。
- **env 重启摩擦**：M1 不改"改 env 需重启"（架构 §4.2）；控制台只管 `auth_ref`，真 key 引入需重启。文档与 UI 提示到位。
- **路由解析口径（R3 已定稿）**：`model_routes.providers` = LiteLLM 模型串列表，`split('/')[0]` 现切前缀；ARCHITECTURE §5 已回写对齐。
- **M1 fallback 是朴素版**：顺序试候选，无错误 code 分类/退避/熔断（M3）。勿因"能切换"误判配额感知路由已完成。
- **Redis 可选**：M1 健康过滤在 `REDIS_URL` 未配时全 healthy，路由仅靠 `providers.enabled`；引入 Redis 与真实标记在 M2/M3。
- **引导安全默认（R5）**：`accounts` 空且缺 `BOOTSTRAP_ADMIN_PASSWORD` 必须启动失败，绝不生成弱默认口令。
- **不提前做**：M1 严禁实现用量落库(M2)、额度扣减(M2)、错误 code 分类/重试/熔断/探活(M3)、成本报表(M3)、语义缓存(M4)。

---

## 6. 下一步

M1 实现完成后，进入 **M2 开发计划**：流式 SSE（含错误补发/客户端断开）、每日 token 额度限流（Redis）、用量日志与归因（SQLite）。将单独产出 `M2_DEV_PLAN.md`。
