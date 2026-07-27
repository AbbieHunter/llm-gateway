# M0 开发计划评审会（模拟）

> 日期：2026-07-23
> 评审对象：`../technical/M0_DEV_PLAN.md` v0.1
> 参会：产品（PM）、开发（Dev）、测试（QA）、用户（User）
> 结论：**通过，回写 v0.2**（6 项修订）

---

## 🎬 会议记录

**PM**：M0 是骨架验证，不是全功能。今天过 `../technical/M0_DEV_PLAN.md`，重点是**别让这个"验证型"里程碑埋下返工雷**——尤其错误映射、model 缺省、Redis 占位、health 语义这几个。

### 议题 1：错误映射现在是"裸的"，M3 要返工吗？

**Dev**：§2.2.1 说"捕获 LiteLLM 异常 → OpenAI 错误 JSON + 透传 HTTP 状态"。但 M3 的**配额感知路由**核心就是错误 code 映射（`QUOTA_EXHAUSTED` 等四类）。如果 M0 只是 `except Exception` 一把梭，M3 得回来把整个异常处理拆了重做。LiteLLM 抛的是 `litellm.exceptions.APIError`，带 `.status_code` 和 `.message`，结构很规整。

**PM**：我理解你的担心——但 M0 定位是"验证改 base_url 能调通"，不想引 M3 的东西进来变重。

**Dev**：不是引 M3 逻辑，是**现在就把接缝种上**：建一个瘦的 `core/errors.py`，M0 只做"LiteLLM 异常 → OpenAI JSON"这一层，但枚举/结构按 M3 的四类留好扩展位。成本几乎为零，M3 直接往里填 case，不返工。

**User**：（点头）我赞成种接缝。但别在 M0 就把配额那套判断逻辑写出来，那是我 M3 才要的。

**QA**：+1，我测试也想要一个稳定的错误出口，不然每次异常格式飘，我用例没法写。

**PM**：好，定：**M0 种瘦 `core/errors.py` 接缝（异常→OpenAI JSON），不写配额判断逻辑，M3 填 case。**

### 议题 2：`model` 缺省怎么办？

**Dev**：§2.2.1 写"M0 不强校验全部字段"，又写"passthrough 以请求体 `model` 为准"。那请求不带 `model` 呢？现在代码会直接把空 `model` 扔给 LiteLLM，报一堆厂商侧的怪错，不是 OpenAI 风格。

**PM**：OpenAI 本身 `model` 是必填。我们该对齐——缺了就 400，返回 OpenAI 错误 JSON，别静默默认某个模型。

**User**：对，静默默认反而坑人。我哪天手滑漏了 `model`，宁可立刻 400 告诉我，也别默默用了个贵模型烧我钱。

**QA**：那 AC 加一条：缺 `model` → 400 + OpenAI 错误体；传非法 `model` → 厂商 4xx 透传。

**PM**：采纳。**`model` 必填，缺失即 400（OpenAI 风格）；不做静默默认值。**

### 议题 3：Redis 占位随 M0 起，是不是个僵尸容器？

**Dev**：§2.4 的 compose 把 redis 拉起来了，但 M0 完全不用它。一个永远闲着的容器，部署误解、监控误报，纯负担。

**PM**：但架构 §4 说 Redis 是限流/缓存底座，放 compose 是"宣示未来"。

**Dev**：宣示可以写在文档里，不必真起一个空容器。M2 真正用的时候再加，diff 更干净。

**QA**：我同意 Dev，测试环境少一个无谓依赖，起停更快。

**User**：我无所谓，别让我为了跑 M0 还得管一个用不上的 Redis。

**PM**：行，**M0 compose 去掉 redis 服务**；在文档注明"Redis 将于 M2 引入，届时加入 compose"，把"宣示"留在文字里。

### 议题 4：`/healthz` 绿了 ≠ 模型能用，这个歧义得消掉

**QA**：DoD 写"`/healthz` 返回 200"就算启动成功。但这是 app 存活，不代表 provider key 有效、模型能调。要是我把 health 绿当成"网关可用"，半夜 key 过期了我还以为没事。

**Dev**：标准做法：`/healthz` = liveness（进程在），另留 `/health`（或 `/readyz`）= readiness（依赖可用）。M0 只做 liveness，但文档必须写清楚它**不探测 provider**。

**PM**：明确写上：**`/healthz` 仅表进程存活，不探测上游 provider 可达性**；provider 探针留到 M2（限流/缓存依赖 Redis 时一起做）。QA 验收时不能把 health 绿等同于"能对话"。

**QA**：好，那我在 M0 的验收里分两层：health 绿（liveness）+ 实际 SDK 对话成功（真能调）。

### 议题 5：前端没构建时，`/` 会 404 还是崩？

**Dev**：§2.3 的 `mount("/", StaticFiles(directory="app/static/dist"))`——如果 `dist` 还没 `npm run build` 生成，目录不存在，FastAPI 起服务就直接抛 `RuntimeError`。新人 clone 下来不构建前端，后端都起不来，体验很差。

**User**：对，我最怕"按文档一步步来结果第一步就报错"。

**PM**：两个解法：①把"前端必须先 build"写进 README/启动步骤；②给个兜底——`dist` 不存在时用仓库内一个最小 `index.html` 占位，保证 `/` 不崩。

**Dev**：我倾向②兜底 + ①文档。兜底成本低，且和"占位不投样式"的精神一致——反正 M0 就是个占位页。

**QA**：兜底页也顺便当 T-04 的"未构建也能看到占位"的验收。

**PM**：定：**仓库提交一个最小 `app/static/dist/index.html` 兜底占位**（保证 `/` 不崩），同时文档写清"正式前端需 `npm run build` 产出 dist"**；T-04 验收加"未构建时 `/` 仍返回兜底页"。

### 议题 6：M0 怎么验收，纯手测还是给个冒烟脚本？

**QA**：DoD 列的都是手动 curl / 开浏览器。M0 虽小，但这是整个项目的第一根线，建议给个**冒烟脚本**（`scripts/smoke.sh`：探 healthz + 发一次 chat），CI 以后也能接。

**Dev**：同意，脚本我写，_owner 我 + QA 联调。其实就是把 DoD 的步骤脚本化。

**PM**：加为 **T-06 冒烟脚本**，归入 DoD。T 恤尺码 S。

**User**：挺好，至少证明"改 base_url 真能调通"是可重复的，不是我手测一次就过了。

### 议题 7：M0 的 T 恤尺码现在能锁定吗？

**PM**：文档写"待 Dev 在 M1 前确认"。但 M0 现在就在排，M1 还没动，M0 这几个尺码能不能现在定？

**Dev**：能。S/M/M/M/M 我认可——T-01 脚手架 S、T-02 passthrough M、T-03 models S、T-04 SPA 托管 M、T-05 容器化 M、新增 T-06 冒烟 S。M1 的尺寸等写 M1 计划时再估。

**PM**：好，**M0 尺码锁定**，M1 另估。

---

## ✅ 最终决议（全员通过）

1. **种错误映射接缝**：新增瘦 `core/errors.py`，M0 仅做 LiteLLM 异常→OpenAI JSON，结构按 M3 四类预留；**不写配额判断逻辑**。
2. **`model` 必填**：缺失 → 400 + OpenAI 错误体；不做静默默认。
3. **M0 compose 去 Redis**：服务移除，文档注明 M2 引入；消除僵尸容器。
4. **`/healthz` 语义明确**：仅进程 liveness，不探测 provider；provider 探针留 M2。
5. **前端兜底页**：提交最小 `app/static/dist/index.html` 占位，未构建时 `/` 不崩；文档写清正式前端需 build。
6. **新增 T-06 冒烟脚本**（S），归入 DoD。
7. **M0 T 恤尺码锁定**：S/M/M/M/M/S（含 T-06）。

**显式不做（防超范围，延续前序决议）**：VK 鉴权、别名路由、流式、配额、DB、RBAC、精致前端样式——全部不在 M0。

---

## 行动项

| # | 项 | 负责人 | 落点 |
|---|----|--------|------|
| 1 | 加 `core/errors.py` 接缝 | Dev | M0_DEV_PLAN §2.2 |
| 2 | `model` 必填校验 + 400 | Dev | §2.2.1 / T-02 |
| 3 | compose 删 redis 服务 | Dev | §2.4 / T-05 |
| 4 | healthz 语义注释 + 未来 /health | Dev | §2.4 |
| 5 | 提兜底 index.html + 文档 | Dev | §2.3 / T-04 |
| 6 | 加 T-06 冒烟脚本 | Dev+QA | §3 新增 |
| 7 | 尺码锁定 S/M/M/M/M/S | Dev | §3 |
