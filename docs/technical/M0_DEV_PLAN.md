# M0 开发任务拆解与技术文档（Dev Plan）

> 版本：v0.2
> 日期：2026-07-23
> 范围：M0 骨架验证 —— 单 provider 非流式跑通 + 控制台 SPA 空壳 + 一键本地启动
> 变更：v0.2 评审回写 —— 种错误映射接缝、model 必填 400、compose 去 Redis、healthz 语义明确、前端兜底页、新增 T-06 冒烟脚本、尺码锁定。
> 对应故事：`../product/USER_STORIES.md` US-M0-01 / US-M0-02 / US-M0-03
> 关联：`ARCHITECTURE.md` v0.2 §7 目录结构、§3 技术选型、§4.1 接入层
> 定位：本文档是 M0 的**任务拆解 + 技术规格**，不含代码实现。M1+ 将单独成篇。

---

## 1. M0 范围与目标

**目标一句话**：证明"改一个 `base_url` 就能调用大模型"的统一入口成立，且控制台前端能被单进程托管、项目能一键起。

| 故事 | 目标 | 不在 M0 的事（防超范围） |
|------|------|--------------------------|
| US-M0-01 统一入口非流式 | `/v1/chat/completions`（非流式）经 LiteLLM 调通单 provider，严格 OpenAI 兼容 | 不含 VK 鉴权、不含别名路由、不含流式、不含配额 |
| US-M0-02 控制台可加载 | FastAPI `StaticFiles` 托管前端构建产物，`/` 返回 SPA | 不含任何管理功能、不投样式（占位即可） |
| US-M0-03 一键启动 | `docker-compose up` 起网关 + Redis，健康检查通过 | 不含多实例、不含生产 TLS |

**M0 完成判据（DoD）**：用官方 OpenAI SDK 仅改 `base_url` 完成一次非流式对话；浏览器打开根路径看到控制台占位页；`docker-compose up` 后 `/healthz` 返回 200。

---

## 2. 技术设计（M0）

### 2.1 目录结构与脚手架

按 `ARCHITECTURE.md` §7 单仓库单进程布局，M0 先落地最小骨架：

```
llm-gateway/
├── app/
│   ├── main.py                 # FastAPI 入口：挂载 /v1、SPA 静态、/healthz
│   ├── routers/
│   │   └── openai.py           # /v1/chat/completions, /v1/models
│   ├── core/
│   │   ├── adapters.py         # LiteLLM 封装（M0: 直接 passthrough）
│   │   └── errors.py           # 错误映射接缝（M0: LiteLLM异常→OpenAI JSON；M3 填配额 case）
│   └── config.py               # 读 env（provider 凭据、默认 model）
├── frontend/                   # React + Vite + Tailwind 空壳
│   ├── index.html
│   ├── vite.config.ts          # build → ../app/static/dist
│   └── src/main.tsx            # 单个占位页
├── app/static/dist/            # 前端构建产物（gitignore，运行时生成）
│   └── index.html              # 兜底占位页（仓库提交，保证未构建时 / 不崩）
├── requirements.txt            # fastapi, uvicorn, litellm, pydantic, redis(后续)
├── Dockerfile
├── docker-compose.yml          # 网关 + redis
└── .env.example                # OPENAI_API_KEY 等示例
```

> M0 不引入 DB / Redis 业务依赖（Redis 仅为 compose 占位，M2 才用）；`app/db`、`middleware`、`core/router.py` 等 M1+ 再建。

### 2.2 网关 API（/v1）设计

**M0 路由策略**：直接 passthrough —— 请求体里的 `model` 原样传给 LiteLLM，由 LiteLLM 按 env 中的 provider key 决定实际厂商。"单 provider"是**部署/测试范围**（只配一个 key），不是代码约束。这样 M0 就能验证"改 base_url 调多家"的雏形，M1 再加别名映射与 VK 鉴权。

#### 2.2.1 `POST /v1/chat/completions`
- **请求**：OpenAI ChatCompletion 格式。M0 不强校验全部字段，关键字段：`model`(str, **必填**)、`messages`(list)、`stream`(bool, M0 仅支持 `false`)、`temperature`/`max_tokens`/`top_p` 等透传。
- **`model` 校验（评审决议）**：`model` 缺失 → 立即返回 **400** + OpenAI 错误 JSON（`{"error":{"message":"model is required","type":"invalid_request_error","code":"missing_model"}}`）。**不做静默默认值**——防止漏传 model 时误调贵模型。非法 `model`（厂商侧 4xx）按下方错误映射透传。
- **处理**：
  ```python
  import litellm
  from app.core.errors import map_litellm_error  # 接缝：M0 仅映射，M3 填配额 case
  if not request.model:
      raise HTTPException(status_code=400, detail=openai_error("model is required", "invalid_request_error", "missing_model"))
  try:
      resp = await litellm.acompletion(
          model=request.model,
          messages=request.messages,
          stream=False,
          **passthrough_kwargs,   # temperature 等
      )
  except litellm.exceptions.APIError as e:
      raise map_litellm_error(e)   # → OpenAI 风格 JSON + 透传 HTTP 状态
  return resp.model_dump()    # litellm 返回即 OpenAI 兼容对象
  ```
  > M0 直接序列化 LiteLLM 响应（其结构天然 OpenAI 兼容）。M1+ 再用 Pydantic 做严格校验与归一化。
- **响应**：与 OpenAI 一致（`id/object/created/model/choices/usage`）。
- **错误映射接缝（评审决议）**：新增 `app/core/errors.py`，M0 仅做"LiteLLM 异常（`litellm.exceptions.APIError` 等）→ OpenAI 错误 JSON + 透传 HTTP 状态"这一层。结构按 `ARCHITECTURE.md` §4.4 的四类（`QUOTA_EXHAUSTED`/`RATE_LIMITED`/`AUTH_ERROR`/`UPSTREAM_5XX`）预留扩展位，**M0 不写配额判断逻辑**，M3 直接往里填 case，避免返工。

#### 2.2.2 `GET /v1/models`
- 返回网关可见模型列表（OpenAI `/v1/models` 格式）。
- M0 简化：返回配置中的默认模型（或 LiteLLM 已知模型的一个静态子集），`id` 即模型名。后续 M1 由 `model_routes` 别名驱动。

### 2.3 前端 SPA 空壳（US-M0-02）

- **脚手架**：`npm create vite@latest frontend -- --template react-ts`；装 Tailwind（仅基础，不投样式精力）。
- **页面**：单个占位页（如 `<h1>LLM Gateway 控制台</h1>` + "建设中"提示），**不实现任何管理功能、不做精致样式**（评审决议）。
- **构建**：`vite.config.ts` 设 `build.outDir = '../app/static/dist'`，`base = './'`（相对路径，便于 StaticFiles 托管）。
- **兜底页（评审决议）**：仓库提交一个最小 `app/static/dist/index.html` 占位（标题 + "建设中"）。原因：`StaticFiles(directory=...)` 在目录不存在时 FastAPI 启动即抛 `RuntimeError`，新人 clone 未构建前端会导致后端也起不来。兜底页保证**未构建时 `/` 也能返回占位页，不崩**；正式前端经 `npm run build` 覆盖 dist 即可。
- **托管（关键）**：FastAPI 中
  ```python
  app.mount("/", StaticFiles(directory="app/static/dist", html=True), name="spa")
  ```
  - **路由优先级**：`/v1/*` 与 `/healthz` 必须在 `mount("/")` **之前**注册，否则 SPA catch-all 会吞掉 API。
  - `html=True` 使未知路径回退 `index.html`（SPA 路由预留）。

### 2.4 部署与一键启动（US-M0-03）

- **Dockerfile**：基于 `python:3.13-slim`，装 `requirements.txt`，`CMD uvicorn app.main:app --host 0.0.0.0 --port 8000`。
- **docker-compose.yml（评审决议：去 Redis）**：
  ```yaml
  services:
    gateway:
      build: .
      ports: ["8000:8000"]
      env_file: .env
  ```
  > **M0 不随 compose 起 Redis**（评审决议：避免僵尸容器）。Redis 作为限流/缓存底座将于 **M2** 引入，届时再加入 compose 并配 `depends_on`。
- **健康检查语义（评审决议）**：`GET /healthz` → `{"status":"ok"}`，200。该端点**仅表进程存活（liveness），不探测上游 provider 可达性**。provider / 依赖探针（readiness）留到 M2（与 Redis 引入一并做）。QA 验收时**不得将 healthz 绿等同于"模型能调通"**——需以实际 SDK 对话成功为准。

### 2.5 配置与依赖

- **环境变量（凭据不落库，见架构 §3/§4.2）**：
  - `OPENAI_API_KEY`（或对应厂商 key，如 `DEEPSEEK_API_KEY`）
  - `DEFAULT_MODEL`（可选，M0 未强制；passthrough 时以请求体 `model` 为准）
- **requirements.txt**：`fastapi`、`uvicorn[standard]`、`litellm`、`pydantic`、`python-dotenv`。
- **.env.example**：列出上述变量，注明"新增 provider 需配 env 并重启"（架构 §4.2 约束，M0 即生效）。

### 2.6 验收映射

| 故事 AC | M0 设计落点 |
|---------|-------------|
| US-M0-01：SDK 改 base_url 能对话、OpenAI 兼容 | §2.2.1 passthrough + litellm 响应直出 |
| US-M0-02：`/` 返回 SPA、骨架可渲染 | §2.3 StaticFiles 挂载 + 路由优先级 |
| US-M0-03：单命令启动、/healthz 通过 | §2.4 compose + 健康检查 |

---

## 3. 开发任务拆解（Task Breakdown）

> T 恤尺码（S/M/L）**M0 已锁定**（评审会确认）：S/M/M/M/M/S。M1 尺码将于写 M1 计划时另估。每个任务含可勾选子项与对应故事/AC。

### T-01 · 项目脚手架与 FastAPI 入口 — 【S】
- 对应：US-M0-03（基础设施）
- [ ] 建 `app/` 包与 `main.py`，可 `uvicorn app.main:app` 起服务
- [ ] `requirements.txt` / `.env.example` 就位
- [ ] `app/config.py` 读 env（provider key、默认 model）
- [ ] 注册 `GET /healthz` 返回 200
- 验收：`curl /healthz` 返回 `{"status":"ok"}`

### T-02 · `/v1/chat/completions` 非流式 passthrough — 【M】
- 对应：US-M0-01
- [ ] `app/routers/openai.py` 定义端点，接收 OpenAI 格式请求体
- [ ] `app/core/adapters.py` 封装 `litellm.acompletion(model=..., stream=False, ...)`
- [ ] 成功：返回 `resp.model_dump()`（OpenAI 兼容）
- [ ] 失败：捕获 LiteLLM 异常 → OpenAI 错误 JSON + 透传 HTTP 状态
- [ ] 本地用 OpenAI SDK 改 `base_url` 跑通一次非流式对话（单 provider key）
- 验收：满足 US-M0-01 全部 AC

### T-03 · `GET /v1/models` — 【S】
- 对应：US-M0-01
- [ ] 返回 OpenAI `/v1/models` 格式列表（M0 含默认/已知模型）
- 验收：SDK `client.models.list()` 可解析

### T-04 · 前端 SPA 空壳与托管 — 【M】
- 对应：US-M0-02
- [ ] `frontend/` Vite react-ts 脚手架 + Tailwind 基础
- [ ] 单个占位页（标题 + "建设中"），**不投样式精力**
- [ ] `vite.config.ts`：`outDir=../app/static/dist`，`base='./'`
- [ ] FastAPI 在 `/v1`、`/healthz` **之后** `mount("/", StaticFiles(html=True))`
- [ ] 构建产物生成并能被 `/` 返回
- 验收：浏览器开 `/` 看到占位页；`/v1/models` 仍正常（路由优先级正确）

### T-05 · Docker 化与一键启动 — 【M】
- 对应：US-M0-03
- [ ] 写 `Dockerfile`（python:3.13-slim + uvicorn）
- [ ] 写 `docker-compose.yml`（**仅 gateway，不含 redis**，见 §2.4）
- [ ] compose `healthcheck` 探 `/healthz`
- [ ] `.env` 提供测试用 provider key（或文档指引）
- 验收：`docker-compose up` 后 `/healthz` 200，SDK 经容器 base_url 对话成功

### T-06 · 冒烟脚本（Smoke Test） — 【S】
- 对应：US-M0-01 / US-M0-03（验收自动化）
- [ ] `scripts/smoke.sh`：探 `/healthz` 200 → 用 OpenAI SDK 改 `base_url` 发一次非流式 chat → 断言响应含 `choices`
- [ ] 覆盖缺 `model` 场景：断言返回 400 + OpenAI 错误体
- [ ] 文档标注该脚本可接入后续 CI
- 验收：本地与容器内各跑一次全绿；QA 验收以脚本 + 实际对话双层为准

### 依赖关系
```
T-01 (脚手架/healthz) ──▶ T-02, T-03 (网关 API)
T-01 ──▶ T-04 (前端托管依赖 StaticFiles 挂载点)
T-01, T-02, T-04 ──▶ T-05 (容器化打包)
T-02, T-05 ──▶ T-06 (冒烟脚本依赖可运行网关)
```
> T-02 与 T-04 可并行；T-05 收口；T-06 最后联调。

---

## 4. M0 完成定义（Definition of Done）

- [ ] `docker-compose up` 一键起网关（**不含 Redis**），无致命日志
- [ ] `/healthz` 返回 200（**仅表进程存活，不表 provider 可用**）
- [ ] 官方 OpenAI SDK 仅改 `base_url` 完成一次非流式对话，响应 OpenAI 兼容
- [ ] 浏览器打开 `/` 加载控制台占位 SPA（含未构建时的兜底页）；`/v1/*` 不被 SPA 吞掉
- [ ] 缺 `model` → 400 + OpenAI 错误体；非法 model → 厂商 4xx 透传（OpenAI 风格，非 500 裸栈）
- [ ] `scripts/smoke.sh` 本地与容器内各跑一次全绿
- [ ] 凭据走 env，`.env.example` 标注"新增 provider 需重启"
- [ ] `core/errors.py` 接缝就位（M0 仅映射，M3 填配额 case）

---

## 5. 风险与注意

- **路由优先级陷阱**：`mount("/", StaticFiles)` 必须最后注册，否则会吞掉 `/v1`。T-04 验收重点。
- **前端未构建即起服务**：`dist` 目录不存在时 FastAPI 启动会抛 `RuntimeError`；已用仓库兜底 `index.html` 化解（见 §2.3），但仍需在 README 写清"正式前端需 `npm run build`"。
- **healthz 语义误读**：`/healthz` 仅表进程存活，**不代表 provider 可用**；QA 验收需以实际 SDK 对话为准，勿将 health 绿等同于"网关可用"。
- **LiteLLM 版本**：固定版本号，避免 `litellm` 大版本改动破坏 passthrough 行为。
- **不提前做**：M0 严禁引入 VK 鉴权、别名、流式、配额、DB、Redis——这些在 M1/M2/M3，文档已划清。
- **前端投入度**：占位即可，不要在 M0 花工时做组件/布局（评审明确）。
- **错误映射接缝**：M0 只建映射层、不写配额判断；切勿在 M0 提前实现 `QUOTA_EXHAUSTED` 等 M3 逻辑。

---

## 6. 下一步

M0 实现完成后，进入 **M1 开发计划**：账号体系（US-M1-00 初始管理员引导、US-M1-01 登录、US-M1-05 RBAC）+ 虚拟 Key + Provider + 路由别名 + 控制台基础页。将单独产出 `M1_DEV_PLAN.md`。
