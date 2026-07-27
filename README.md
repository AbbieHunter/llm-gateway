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

## 控制台操作（简明指南）

控制台即 http://localhost:8000 的网页，登录后可见以下页面：

### 1. 仪表盘（Dashboard）
- 顶部四张概览卡：活跃虚拟 Key 数、今日请求数、今日 token 消耗、估算成本。
- **近期异常**：列出运行中被标记为 `quota_exhausted` / `degraded` 的模型或 Provider。点「重置」即可清除其运行时状态、恢复候选资格（无需重启）。

### 2. Provider（Providers）
- 查看 / 新增 Provider **前缀**（如 `openai`、`deepseek`）。
- **这里只填前缀，不填密钥** —— 真实 API Key 写在服务器 `.env` 中（`OPENAI_API_KEY` 等），网关经环境变量读取。

### 3. 路由（Routes）—— 建别名
- 新建一个**别名（alias）**（如 `free`），作为调用时的 `model` 名。
- 选**策略**：
  - `failover`：候选按列表顺序优先级，前一模型额度耗尽/失败自动切下一个。
  - `weighted`：按权重比分配。
- 填**候选模型**（完整串，逗号或列表分隔）：
  ```
  openai/qwen-plus-2025-12-01
  openai/qwen-max
  ```
- 保存后，调用方用 `model: "free"` 即可命中这条路由。

> 配额感知是按**完整模型串**粒度的：别名里某个模型额度耗尽，只会跳过那一个，其余模型继续服务（不会把整个 `openai` 前缀打挂）。

### 4. 虚拟 Key（Keys）
- 新建虚拟 Key：填写名称、归属账号、每日 token 限额（测试期可设大，避免在上游 1M 额度前就被网关 429 拦住）。
- 创建后**明文 Key 仅显示一次**，务必复制保存。
- 支持重置（重新生成）、删除；普通用户的 Key 按归属账号强制隔离（RBAC 后端为唯一真相源）。

### 5. 用量（Usage）
- 三维用量报表：按 Key / 模型 / 账号切换查看请求数、token、估算成本。
- 支持 **CSV 导出**（90 天窗口、10 万行上限；导出做了公式注入防护）。

### 6. 账号（Accounts，管理员）
- 新建 / 管理账号与角色（admin / user）。无公开注册页。

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
