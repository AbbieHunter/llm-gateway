# LLM Gateway — 生产基础设施（redis + bge 语义缓存 embedding 服务）

本目录把 **真实 Redis**（M2 起每日 token 额度计数 / 用量归因 / 缓存 / 熔断状态必需）
和 **bge-small-zh-v1.5 embedding 服务**（M4 语义缓存的真实向量来源）用 docker-compose 一键拉起。

网关本身既可以在 compose 内一起跑，也可以在宿主机直接 `uvicorn` 跑（连 localhost 映射端口）。

## embedding 后端：Ollama（默认，免 torch 构建）
compose 默认用官方 `ollama/ollama` 镜像，它原生提供 OpenAI 兼容的 `/v1/embeddings`，
并由 `embedding-pull` 服务在首次启动时自动 `ollama pull bge-small-zh-v1.5`。
**不需要构建 PyTorch 镜像**，更轻、启动更快，生产的推荐做法。

> 想要自托管 torch 服务（sentence-transformers）？见 `deploy/embedding-server/`（需能拉取
> PyTorch CPU 轮子，构建较重）。把 compose 的 `embedding`/`embedding-pull` 换成该 build 上下文即可。

## 一键启动全栈（redis + embedding + gateway）
```bash
# 1) 编辑仓库根 .env：至少填 BOOTSTRAP_ADMIN_PASSWORD 与真实 provider key
vim .env

# 2) 构建并后台启动（会拉 redis + ollama 镜像，并自动下载 bge 模型）
docker compose up -d --build

# 3) 等 healthy（ollama 拉模型可能数十秒~几分钟，取决于网速）
docker compose ps

# 4) 探活
curl -s localhost:8000/healthz          # 网关
curl -s localhost:8001/api/tags         # ollama 已加载的模型列表（应含 bge-small-zh-v1.5）
redis-cli -h localhost -p 6379 ping     # redis -> PONG
```

## 只起基础设施、网关跑在宿主机（你一直的习惯）
```bash
# 只启动 redis + embedding（不启动 compose 里的 gateway）
docker compose up -d redis embedding embedding-pull

# 然后在本机用 .env 跑网关，关键变量改成宿主机可达地址：
#   REDIS_URL=redis://localhost:6379
#   REDIS_FAKE=0
#   MOCK_PROVIDER=0
#   SEMANTIC_EMBEDDING_MODEL=bge-small-zh-v1.5
#   SEMANTIC_EMBEDDING_API_BASE=http://localhost:8001/v1
uvicorn app.main:app --port 8000
```

## 端口映射
| 服务 | 容器端口 | 宿主机端口 | 用途 |
|------|---------|-----------|------|
| redis | 6379 | 6379 | 额度/缓存/状态 |
| embedding (ollama) | 11434 | 8001 | `/v1/embeddings`（bge-small-zh-v1.5） |
| gateway | 8000 | 8000 | OpenAI 兼容网关 |

## 换模型
改 `docker-compose.yml` 里 `embedding-pull` 的 `ollama pull <model>` 与网关 `SEMANTIC_EMBEDDING_MODEL`。
注意：语义缓存按 `(provider/model)` 作用域隔离，换模型后旧缓存不会跨模型命中（符合设计）。

## 降级说明
embedding 服务不可用 / `SEMANTIC_EMBEDDING_API_BASE` 为空时，语义层**优雅降级**
（embedding 失败 → 跳过语义层走上游，不阻断请求，只是不再命中语义缓存）。
所以即使 embedding 服务没起来，网关也能正常服务，只是语义缓存不生效。

---

## HA / 滚动发布（根治部署空窗）

云上生产推荐路径：[`docker-compose.prod.ha.yml`](../docker-compose.prod.ha.yml)
（Postgres + `gateway-a` + `gateway-b` + Redis，无宿主机端口，挂在 `card-rules-assistant_default`）。

```
Clients → Caddy /gw → gateway-a + gateway-b → Redis + Postgres
```

日常发版只滚动替换一台，另一台继续接流量：

```bash
cd ~/workspace/llm-gateway
./scripts/rollout_ha.sh
```

**禁止**对 HA 使用 `docker compose -f docker-compose.prod.ha.yml up -d --build`
（会同时 recreate 两台，重新制造空窗）。

### 首次从「单 gateway + SQLite」切换（防空窗顺序）

前置：在服务器 `.env` 写入 `POSTGRES_PASSWORD`（字母数字为宜，避免 `@ : /`
破坏 compose 拼出的 `DATABASE_URL`）。已有 `BOOTSTRAP_ADMIN_PASSWORD` /
`JWT_SECRET` / Provider key 保持不变。

1. **拉代码**到含 HA 文件的 commit。
2. **只起 Postgres**（此时旧 `gateway` 仍在服务）：
   ```bash
   docker compose -f docker-compose.prod.ha.yml up -d postgres
   ```
3. **短停写 + 迁移**（旧单实例短暂停服窗口）：
   ```bash
   docker compose -f docker-compose.prod.yml stop gateway
   # 在能访问 postgres:5432 的环境跑迁移（示例：临时容器挂到同一网络）
   docker run --rm --network card-rules-assistant_default \
     -v "$PWD/data:/data:ro" -v "$PWD:/app" -w /app \
     -e SOURCE_SQLITE=/data/gateway.db \
     -e DATABASE_URL="postgresql+asyncpg://gateway:${POSTGRES_PASSWORD}@postgres:5432/gateway" \
     python:3.12-slim bash -c "pip install -q -r requirements.txt && python scripts/migrate_sqlite_to_postgres.py"
   ```
   目标库若已有 `accounts` 行，脚本会拒绝覆盖。
4. **起双实例 + Redis**（不要碰旧 Caddy 上游名之前先确认 health）：
   ```bash
   docker compose -f docker-compose.prod.ha.yml up -d redis gateway-a gateway-b
   # 或：./scripts/rollout_ha.sh
   docker compose -f docker-compose.prod.ha.yml ps
   ```
5. **改 Caddy 为双上游**（`card-rules-assistant` 仓库的 Caddyfile）：
   - 参考本目录 [`Caddyfile.gw.ha.snippet`](Caddyfile.gw.ha.snippet)
   - 将 `reverse_proxy gateway:8000` 换成 `gateway-a:8000 gateway-b:8000`
   - reload，例如：
     ```bash
     docker exec card-rules-caddy caddy reload --config /etc/caddy/Caddyfile
     ```
6. **验证**：
   - `curl -sS https://www.asdfghjkl.site/gw/healthz`
   - 控制台登录、一次 chat
   - `docker compose -f docker-compose.prod.ha.yml stop gateway-a` 后 healthz / chat 仍通；再 `start gateway-a`
7. **停掉旧单实例**（若仍在旧 compose 项目中）：
   ```bash
   docker compose -f docker-compose.prod.yml stop gateway
   # 确认无流量后再考虑 rm；不要 docker compose down -v
   ```

顺序口诀：**迁库 → 起双实例 → 改 Caddy → 再停旧 gateway**。
若先停旧实例再改 Caddy，中间会无上游。

### 优雅退出

[`scripts/entrypoint.sh`](../scripts/entrypoint.sh) 使用
`--timeout-graceful-shutdown 30`；HA compose 上
`stop_grace_period: 35s`。滚动时被替换的那台会尽量收尾短请求；
超长 SSE 仍可能断在被摘掉的实例上，另一台继续服务新连接。
