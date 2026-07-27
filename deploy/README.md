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
