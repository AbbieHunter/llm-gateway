# LLM Gateway

自建大模型网关：对外暴露 **OpenAI 兼容协议**（`/v1/chat/completions` 等），对内统一接入多家 Provider，提供**虚拟 Key 鉴权、按别名路由、配额感知故障转移、用量计量与成本报表、可观测面板**。

定位：个人 / 小团队自用，统一入口 + 成本与配额管控 + 高可用。

---

## 特性一览

- **统一接入**：OpenAI 兼容 API；通过 LiteLLM 适配 300+ 模型，Provider 凭证用环境变量注入（不落库）。
- **账号与鉴权**：管理员 / 普通用户 RBAC；虚拟 Key（VK）入库只存 SHA-256 哈希，明文仅创建时返回一次。
- **路由策略**：
  - `failover`（有序优先级 + 配额感知）：某候选模型额度耗尽→只标记该模型，自动切换到同别名下其他可用模型。
  - `weighted`（按权重比分配流量）。
- **高可用**：配额感知故障转移、熔断 + 退避重试、运行时状态自愈探活。
- **流式**：SSE 流式响应；流式中途出错会补发 OpenAI 错误事件，不静默断流。
- **限流**：按虚拟 Key 的**每日 token 硬限额**（本地时区自然日重置）。
- **缓存**：精确缓存（同请求零上游）+ 语义缓存（Tier2，bge 向量相似命中，可插拔 embedding 后端）。
- **成本路由（cost）**：最便宜优先；缺价时兜底。
- **用量报表**：按 Key / 模型 / 账号三维用量 + 估算成本（软观测）+ CSV 导出（90 天窗 / 10 万行限 / 公式注入防护）。
- **可观测**：零依赖 Prometheus `/metrics`（仅聚合、无 VK / PII）。
- **护栏（默认关）**：入站 PII redact / 出站 mask，可独立开关，不落库。
- **管理控制台**：React + Vite SPA，由后端静态托管（桌面优先）。

> 北极星指标：月度活跃虚拟 Key 数（MAK）。
> 非目标：不做多租户 SaaS、不做模型微调 / 私有部署、不做强制 PII 护栏。

---

## 架构（简述）

```
客户端 ──► [鉴权中间件: VK/会话] ──► OpenAI 兼容路由 (/v1)
                                         │
                ┌────────────────────────┼─────────────────────────┐
                │  治理层（自研）          │                          │
                │  限流 / 计量 / 缓存 /    │                          │
                │  路由策略 / 熔断 / 护栏  │                          │
                └────────────────────────┼─────────────────────────┘
                                         ▼
                               LiteLLM 适配层 ──► 各 Provider（env 凭证驱动）
```

- 框架：FastAPI；校验：Pydantic v2；缓存/限流：Redis；元数据存储：SQLite（个人/小团队够用）。
- Provider 凭证经**环境变量**注入（如 `OPENAI_API_KEY` / `OPENAI_API_BASE`），新增 Provider 需改 `.env` + 重建容器。
- 运行时状态（healthy / degraded / down / quota_exhausted）存 Redis，按**完整候选模型串**粒度（如 `openai/qwen-plus-2025-12-01`），不落关系库。

---

## 快速开始（Docker Compose，推荐）

### 前置
- Docker + Docker Compose
- 一个真实 Provider 的 API Key（如阿里云百炼 DashScope 的兼容端点）

### 步骤
1. 复制环境变量模板并填写：
   ```bash
   cp .env.example .env
   ```
   编辑 `.env`（至少填以下四项，详见下节）：
   - `BOOTSTRAP_ADMIN_PASSWORD`（首次启动创建管理员，缺省则启动失败）
   - `JWT_SECRET`（用 `openssl rand -hex 32` 生成随机值）
   - `OPENAI_API_KEY`（你的 Provider key）
   - `OPENAI_API_BASE`（第三方兼容端点，见下）

2. 启动全栈（Redis + Ollama 语义缓存 embedding + Gateway）：
   ```bash
   docker compose up -d
   ```

3. 检查健康：
   ```bash
   curl http://localhost:8000/healthz      # 应返回 ok
   curl http://localhost:8000/api/tags     # Ollama 模型列表（含 bge-small-zh-v1.5）
   ```

4. 打开控制台：浏览器访问 **http://localhost:8000**，用 `admin` + 你设置的 `BOOTSTRAP_ADMIN_PASSWORD` 登录。

> ⚠️ **改 `.env` 后必须 `docker compose up -d` 重建容器**，`docker compose restart` 不会重载环境变量。

### 数据持久化（重要，别踩坑）

网关的**所有控制台业务数据**——账号、虚拟 Key、路由别名（alias）、Provider 前缀、用量记录——都存储在容器内的 SQLite 数据库 `./data/gateway.db`（`WORKDIR=/app`，即 `/app/data/gateway.db`）。

`docker-compose.yml` 已为 `gateway` 服务挂载了卷：

```yaml
services:
  gateway:
    volumes:
      - ./data:/app/data   # 元数据持久化到宿主机，重建容器不丢失
```

因此：

- ✅ **`docker compose up -d` 重建容器后，数据仍在**（卷在宿主机的 `./data` 目录）。第一次部署时会自动创建 `./data` 目录。
- ✅ **备份很简单**：停止服务后直接拷贝宿主机上的 `./data/gateway.db` 即可（该目录已被 `.gitignore` 排除，不会进 git）。
- ⚠️ **`docker compose down -v` 会删除命名卷**；若你执行了带 `-v` 的 down，SQLite 数据会丢失（Redis 的 `redis-data`、Ollama 的 `ollama-data` 同理）。日常重启用 `docker compose up -d` / `restart` 即可，不要加 `-v`。
- ℹ️ **运行时状态与计数不在此库**：额度耗尽标记、熔断状态、语义缓存、用量计数都存于 **Redis**（已挂 `redis-data` 卷）。即使元数据库重建，这些也不受影响。

> 真实教训：早期 `gateway` 服务**没有挂卷**，数据库只活在容器可写层，一次 `docker compose up -d` 重建容器后，新建的路由别名全部丢失（空库会按 `BOOTSTRAP_ADMIN_PASSWORD` 重建 admin 账号，但别名/Key 不在 bootstrap 范围内）。现已修复并挂卷，数据可持久保存。

### 第三方兼容端点（以 DashScope 为例）
```
OPENAI_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1
```
模型 id 用完整串：`openai/qwen-plus-2025-12-01`、`openai/qwen-max` 等（前缀 `openai/` 由 LiteLLM 路由，地址取自 `OPENAI_API_BASE`）。

---

## 配置（`.env` 关键项）

| 变量 | 说明 | 默认 |
|------|------|------|
| `BOOTSTRAP_ADMIN_PASSWORD` | 首次启动创建管理员密码（**必填**，否则启动失败） | 空（必填） |
| `BOOTSTRAP_ADMIN_USERNAME` | 管理员用户名 | `admin` |
| `JWT_SECRET` | JWT 签名密钥（HS256），生产务必随机 | `dev-insecure-change-me` |
| `REDIS_URL` | Redis 地址；生产必填（`redis://redis:6379`），留空需 `REDIS_FAKE=1` | 空 |
| `OPENAI_API_KEY` / `OPENAI_API_BASE` | Provider 凭证（env 驱动，不经库/前端） | 空 |
| `MOCK_PROVIDER` | 测试开关，`1`=走 echo mock + fake embedding；生产必为 `0` | `0` |
| `SEMANTIC_EMBEDDING_MODEL` / `API_BASE` / `API_KEY` | 语义缓存 embedding 后端（默认 Ollama `quentinz/bge-small-zh-v1.5`） | `quentinz/bge-small-zh-v1.5` / 空（走 Ollama） |
| `METRICS_ENABLED` | Prometheus `/metrics` 开关 | `1` |
| `GUARDRAILS_ENABLED` | PII 护栏总开关（默认关） | `0` |

---

## 控制台操作（详细说明）

控制台即 http://localhost:8000 的网页，用 `admin` + 你设置的 `BOOTSTRAP_ADMIN_PASSWORD` 登录后可见左侧菜单。**每个菜单项下方都有一行功能说明**（鼠标悬停也有提示），下面按菜单顺序详述各页「是干什么的、怎么操作」。

### 菜单总览（每项的功能说明）

| 菜单 | 谁可见 | 一句话说明（即控制台内展示的文案） |
|------|--------|--------------------------------------|
| 概览 | 所有人 | 全局仪表盘：活跃虚拟 Key、今日请求 / Token、估算成本，以及被标记额度耗尽或降级的异常模型一键重置。 |
| 虚拟 Key | 所有人 | 创建与管理的调用凭证（VK）。明文仅创建时显示一次；可按归属账号设定每日 Token 限额，支持重置与删除。 |
| 用量报表 | 所有人 | 按 虚拟 Key / 模型 / 账号 三维查看请求数、Token 消耗与估算成本，支持 CSV 导出（90 天窗口）。 |
| Provider | 仅管理员 | 配置模型提供方前缀（如 openai / deepseek）。这里只登记前缀，真实 API Key 写在服务器 .env，由网关经环境变量读取，不入库。 |
| 路由别名 | 仅管理员 | 把多个候选模型绑成一个别名（如 free），对外用别名调用。支持 failover（按序故障转移）与 weighted（按权重）策略。 |
| 账号 | 仅管理员 | 管理后台账号与角色（admin / user）。普通用户仅能查看自己的 Key 与用量，后端 RBAC 为唯一权限真相源。 |

> 权限区分：普通用户（user）菜单只显示「概览 / 虚拟 Key / 用量报表」；「Provider / 路由别名 / 账号」仅管理员可见。后端 RBAC 是权限的唯一真相源，前端隐藏菜单只是 UX 层、不是安全边界。

---

### 1. 概览（Dashboard）
**作用**：登录后的首页，一眼看清网关整体运行状态。

- **四张概览卡**：
  - 活跃虚拟 Key 数（当月有调用的 VK，对应北极星指标 MAK 的近似）。
  - 今日请求数。
  - 今日 Token 消耗（含 prompt + completion）。
  - 估算成本（软观测，非硬限额；非 OpenAI 模型为估算值，以 Provider 账单为准）。
- **近期异常**：列出运行中被标记为 `quota_exhausted`（额度耗尽）或 `degraded`（降级）的**具体模型或 Provider**（按完整候选串粒度，如 `openai/qwen-plus-2025-12-01`）。点「重置」即可清除其 Redis 运行时状态、闭合熔断，恢复候选资格——**无需重启网关**。这是上游额度耗尽后的自愈入口。

### 2. Provider（Providers）— 仅管理员
**作用**：登记「模型提供方前缀」，告诉网关有哪些厂商可用。**注意：这里不存任何密钥。**

- 查看 / 新增 Provider **前缀**（如 `openai`、`deepseek`、`anthropic`）。前缀对应 LiteLLM 模型串的第一段（`openai/xxx` 的 `openai`）。
- **真实 API Key 写在服务器的 `.env`**（如 `OPENAI_API_KEY=sk-xxx`、`DEEPSEEK_API_KEY=xxx`），网关启动时经**环境变量**读取，绝不落库、也不经前端传输。新增一个 Provider 前缀后，还要在 `.env` 配好对应 key 并 `docker compose up -d` 重建容器才能生效。
- 第三方 OpenAI 兼容端点还要设 `OPENAI_API_BASE=<base>/v1`（LiteLLM 据此决定 `openai/` 前缀的请求地址）。

### 3. 路由别名（Routes）— 仅管理员
**作用**：把「一个或多个底层模型」包装成一个**对外暴露的别名**，调用方只认别名、不关心背后是哪个模型；同时决定多模型之间如何选、如何故障转移。

- **新建别名（alias）**：例如 `free`，调用时 `model: "free"` 即命中。
- **选策略**：
  - `failover`（故障转移）：候选按列表顺序排队，前一个模型额度耗尽 / 报错 / 不健康就自动切到下一个；**配额感知按完整模型串粒度**——某模型耗尽只跳过那一个，其余继续服务（不会连累同前缀的其他模型）。
  - `weighted`（加权）：按权重比把流量分摊到各候选（适合灰度 / 成本均衡）。
- **填候选模型**（完整串，每行一个或用列表）：
  ```
  openai/qwen-plus-2025-12-01
  openai/qwen-max
  ```
  前缀必须已在 Provider 页登记、且真实存在于该端点。
- 保存后，调用方用 `model: "free"` 即可命中这条路由。

### 4. 虚拟 Key（Keys）
**作用**：给「使用方」发的调用凭证。网关鉴权只看 VK，不直接暴露 Provider key。

- **新建**：填名称、归属账号、每日 Token 限额（硬限额，按本地时区自然日重置；测试期可设大，避免在上游 1M 额度之前就被网关 429 拦住）。
- **明文仅显示一次**：创建后完整 VK 明文只弹窗展示一次，**务必立即复制保存**；之后数据库只存其 SHA-256 哈希。
- **管理**：支持重置（重新生成明文）、删除。普通用户只能看到 / 操作自己归属账号下的 Key（后端按 `owner_account_id` 强制过滤）。

### 5. 用量报表（Usage）
**作用**：看「谁、用了多少、花了多少」，用于成本归因与配额观察。

- **三维切换**：按 虚拟 Key / 模型 / 账号 三种维度查看请求数、Token 消耗、估算成本。
- **CSV 导出**：90 天窗口、单文件 10 万行上限；导出做了**公式注入防护**（避免恶意单元格被当作公式执行）。
- 成本字段为**软观测**（估算标注），不做金额硬限额；以 Provider 实际账单为准。

### 6. 账号（Accounts）— 仅管理员
**作用**：管理后台登录账号与角色，实施 RBAC。

- 新建 / 启用停用 / 删除账号，分配角色 `admin` 或 `user`。
- **无公开注册页**：首个管理员由 `BOOTSTRAP_ADMIN_PASSWORD` 在空库时创建，之后全靠管理员在后台增删。
- 权限边界：user 仅能看自己的 Key 与用量；admin 能看到全部。前端菜单隐藏只是 UX，后端过滤才是安全边界。

---

## 用网关发起请求

拿到虚拟 Key 后，像调 OpenAI 一样调用（把 `free` 换成你的别名，`<VK>` 换成虚拟 Key）：

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer <VK>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "free",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": true
  }'
```

---

## 本地开发（无 Docker）

```bash
# 1. Redis（额度计数必需；无 Redis 可设 REDIS_FAKE=1 用内存双打做测试）
export REDIS_URL=redis://localhost:6379
# 或：export REDIS_FAKE=1

# 2. 环境变量
export BOOTSTRAP_ADMIN_PASSWORD=admin12345
export JWT_SECRET=$(openssl rand -hex 32)
export OPENAI_API_KEY=sk-xxx
export OPENAI_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1
export MOCK_PROVIDER=0

# 3. 后端
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# 4. 前端（另开终端）
cd frontend && npm install && npm run build && npm run dev
```

---

## 已知约束 / 注意

- **无"按模型主动封顶"**：网关只在**上游真的返回额度错误**后才反应式标记 `quota_exhausted`；VK 的每日 token 限额是硬限额（per-key，跨所有模型）。
- **额度错误识别依赖英文串**：`classify_error` 认 `insufficient_quota` / `insufficient_balance` / `quota_exceeded` 等；若 Provider 返回中文"额度不足"或纯 429，会被归为 `RATE_LIMITED`（重试/退避）而非 `QUOTA_EXHAUSTED`，届时需在 `errors.py` 扩关键词。
- **模型须真实存在于该端点**：别名里只放确认存在的模型 id，否则上游 404 会原样返回（按 Plan B 只标记该候选）。
- 非 OpenAI 模型的 token 计数为估算，成本以 Provider 账单为准。

---

## 文档

- `docs/product/`：PRD、产品设计、用户故事
- `docs/technical/`：架构 `ARCHITECTURE.md`、各里程碑开发计划
- `docs/meetings/`：各里程碑评审纪要
- `deploy/`：部署参考（含可选 sentence-transformers embedding 服务）

## 测试

```bash
python scripts/smoke_m3.py   # 路由/故障转移/缓存/配额感知
python scripts/smoke_m4.py   # 语义缓存/cost/CSV/metrics/护栏
```
