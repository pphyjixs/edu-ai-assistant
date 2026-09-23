# Vercel 部署设计

## 1. 部署拓扑

建议在 Vercel 中创建两个 Project，连接同一个 Git 仓库：

```text
Project A: edu-ai-frontend
  Root Directory: frontend
  输出: React 静态资源

Project B: edu-ai-api
  Root Directory: backend
  入口: app/main.py 中的 FastAPI app
  输出: Vercel Python Function
```

前端通过环境变量 `VITE_API_BASE_URL` 指向后端域名。后端 CORS 只允许正式前端域名、本地开发地址和明确的预览域名策略。

## 2. 外部依赖

Vercel 运行实例不作为持久存储，以下能力必须外置：

- PostgreSQL：业务数据和任务状态。
- 对象存储：课件、实验报告和导出文件。
- 向量检索：可使用 PostgreSQL 扩展或独立向量服务。
- 后台任务：使用支持 HTTP 推送或队列消费的任务服务；处理器必须幂等。

比赛第一版可以复用 PostgreSQL 保存向量与任务记录，减少服务数量，但代码仍需通过 repository 和 provider 隔离。

## 3. 环境变量

前端：

```text
VITE_API_BASE_URL
```

后端：

```text
APP_ENV
APP_SECRET_KEY
DATABASE_URL
FRONTEND_ORIGINS
LOGIN_RATE_LIMIT_WINDOW_SECONDS
LOGIN_RATE_LIMIT_MAX_FAILURES
STORAGE_ENDPOINT
STORAGE_BUCKET
STORAGE_ACCESS_KEY
STORAGE_SECRET_KEY
STORAGE_REGION
STORAGE_PATH_STYLE
STORAGE_CONNECT_TIMEOUT_SECONDS
STORAGE_READ_TIMEOUT_SECONDS
STORAGE_UPLOAD_URL_TTL_SECONDS
MATERIAL_MAX_UPLOAD_BYTES
MATERIAL_UPLOAD_CONFIRM_TTL_SECONDS
AI_PROVIDER
AI_BASE_URL
AI_API_KEY
AI_MODEL
EMBEDDING_MODEL
JOB_CALLBACK_SECRET
```

`APP_SECRET_KEY` 是访问令牌的签名密钥，开发环境也必填；长度为 32 字符以上的随机值，三个环境各用一份。`LOGIN_RATE_LIMIT_*` 有默认值（900 秒 / 5 次），不配置时按默认执行。

对象存储：`STORAGE_*` 全部有安全默认值，未配置时上传接口返回 `503 SERVICE_UNAVAILABLE`（`details.component = storage`），不会启动失败。`STORAGE_PATH_STYLE` 默认 `true`（MinIO 与多数自建服务需要），云端 S3 可设为 `false` 使用 virtual-host 寻址；`STORAGE_CONNECT_TIMEOUT_SECONDS` / `STORAGE_READ_TIMEOUT_SECONDS` 默认 3 / 10 秒。`STORAGE_UPLOAD_URL_TTL_SECONDS`（默认 600）与 `MATERIAL_MAX_UPLOAD_BYTES`（默认 52428800）、`MATERIAL_UPLOAD_CONFIRM_TTL_SECONDS`（默认 86400）分别对应契约 4.2 / 4.6 中"可配置"的三项。

SigV4 预签名与 HeadObject 请求都在应用侧完成（`app/storage/s3.py`），不依赖 S3 SDK；桶需预先存在（应用不会在启动期建桶）。本地验收可用 `scripts/verify_storage.py` 一键对 MinIO 运行存储侧用例。

**对象存储 CORS 要求（必配）**：浏览器按预签名地址直传对象存储，跨域预检（`OPTIONS`）会先由存储服务应答。存储服务必须有对目标桶生效的 CORS 规则，否则浏览器 PUT 预检失败、直传无法完成（契约 4.4）。MinIO 可用 `mc cors set ALIAS/BUCKET cors.xml` 设置桶规则；未设置桶规则时，它使用全局 CORS 配置。桶规则示例：

```xml
<CORSConfiguration>
  <CORSRule>
    <AllowedOrigin>https://<前端域名></AllowedOrigin>
    <AllowedMethod>PUT</AllowedMethod>
    <AllowedHeader>Content-Type</AllowedHeader>
    <AllowedHeader>x-amz-checksum-sha256</AllowedHeader>
    <AllowedHeader>If-None-Match</AllowedHeader>
    <ExposeHeader>ETag</ExposeHeader>
    <MaxAgeSeconds>600</MaxAgeSeconds>
  </CORSRule>
</CORSConfiguration>
```

要点：

- `AllowedOrigin` 配置明确的前端来源（与后端 `FRONTEND_ORIGINS` 一致）。预签名 PUT 的凭证在 URL 查询参数中，不使用浏览器发送的 `Authorization` 请求头。
- 浏览器直传只需放行 `PUT`；后端的 `HeadObject` 是服务端请求，不经过浏览器 CORS。`AllowedHeader` 必须包含直传所需的三个头：`Content-Type`、`x-amz-checksum-sha256`、`If-None-Match`。
- 预检成功后浏览器才会发起真正的 PUT；未配置 CORS 的桶会以 `403` 或 `CORS error` 拒绝直传，与后端接口无关。

过期上传清理由独立调度任务执行，不能依赖 Vercel 请求进程常驻。建议每 5 分钟在受信任的后端任务环境运行一次 `python scripts/cleanup_expired_uploads.py`；该命令使用该环境的 `DATABASE_URL` 和 `STORAGE_*`，每次最多处理 100 条超过确认窗口且尚未完成的会话。它先删除孤立对象，再将会话标记为 `expired_at`；删除失败的会话留待下次重试，已完成资料不会进入清理范围。调度频率和单次处理量须按实际上传量监控调整。

已删除资料的对象清理由另一条独立命令执行：`python scripts/cleanup_deleted_materials.py`（建议与过期上传清理同频率调度，例如每 5–15 分钟）。它处理 `material_delete_todos` 中「原 PUT 地址过期 + 缓冲期」已过的待办，删除对象并核查晚到 PUT；失败持续重试，不会留下永久孤立对象。缓冲期由 `MATERIAL_DELETE_BUFFER_SECONDS` 控制（默认 3600 秒）。

**解析 Worker** 是独立常驻进程（契约 5.5），不能部署为 Vercel Serverless 函数（长任务超出执行时长限制）。在受信任的后端任务环境（容器 / 常驻主机 / 平台 Worker）运行：

```bash
python scripts/parse_worker.py
```

Worker 依赖该环境的 `DATABASE_URL`、`STORAGE_*` 与模型 `AI_BASE_URL` / `AI_MODEL`（Chat Completions 兼容端点；`AI_API_KEY` 可选）。可选调优：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `WORKER_POLL_SECONDS` | 5 | 队列空时的轮询间隔 |
| `WORKER_BATCH_SIZE` | 5 | 单批最多领取的任务数 |
| `MATERIAL_PARSE_LEASE_SECONDS` | 300 | 任务租约；崩溃后超过租约的任务由重试解析接口回收 |
| `MATERIAL_PARSE_MAX_CHARS` | 120000 | 送入模型的全文上限，超限任务直接 `FAILED` |
| `MATERIAL_PARSE_CHUNK_CHARS` | 8000 | 送入模型的单块文本上限 |
| `AI_TIMEOUT_SECONDS` | 60 | 模型请求超时 |

`AI_BASE_URL` / `AI_MODEL` 未配置时，Worker 会把领取到的任务置为 `FAILED`（安全提示"未配置模型端点"），不会崩溃或无限重试。

**课程问答（RAG）配套**：首版使用 PostgreSQL `pg_trgm` 文本检索，因此

- 数据库必须启用 `pg_trgm` 扩展；迁移 `0008_chat_qa` 会执行 `CREATE EXTENSION IF NOT EXISTS pg_trgm`，需要数据库超级用户或具备该扩展安装权限的角色。权限受限的环境由 DBA 预先执行 `CREATE EXTENSION pg_trgm;`。
- **开放问答前必须回填已有资料**（片段是解析 Worker 之后才落库的，早先解析完成的 `READY` 资料没有片段、检索不到）：

```bash
python scripts/backfill_material_chunks.py                 # 只补没有片段的资料
python scripts/backfill_material_chunks.py --force         # 连已有片段的资料一并重做
python scripts/backfill_material_chunks.py --batch-size 500  # 每页批量大小
```

该命令按 `(created_at, id)` 游标**遍历全部候选**（`--batch-size` 只是每页批量大小、必须是正整数，不是总上限），幂等：重复执行结果一致，失败只输出安全摘要并保留资料原状态，再次运行只重试仍缺片段的资料；资料在回填期间被删除或状态变化时不会留下可检索片段。退出码 `0` 表示全部处理成功，`1` 表示有失败或应处理而未完成的资料（需重跑或排查），`2` 为参数或配置问题（含 `--batch-size` 非正整数），`3` 为数据库不可达。它依赖 `DATABASE_URL` 与 `STORAGE_*`。

问答复用 Worker 的模型配置（`AI_BASE_URL` / `AI_MODEL` / `AI_API_KEY`）。**模型配置只在真正需要调用模型时检查**：检索不到片段时问答按“无依据”返回 `201`，不受模型配置影响；只有检索到片段而模型端点或模型名缺失才返回 `503 SERVICE_UNAVAILABLE`（契约 6.1 / 6.5 / 6.7）。

**课程练习（Practice）配套**：练习题目由**另一个独立常驻进程**生成，
同样不在 Vercel 请求进程内调用模型：

```bash
python scripts/practice_worker.py
```

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PRACTICE_GENERATE_LEASE_SECONDS` | `300` | 生成任务租约（秒）；崩溃后超过租约的 `RUNNING` 任务可由重试接口回收 |
| `PRACTICE_GENERATE_MAX_CHARS` | `60000` | 送入模型的片段上下文上限（字符），按资料顺序轮询截断，并保证每份资料至少一个片段 |
| `PRACTICE_WORKER_POLL_SECONDS` | `5` | 队列空时的轮询间隔 |
| `PRACTICE_WORKER_BATCH_SIZE` | `5` | 单批最多领取的任务数 |

练习 Worker **不需要对象存储**：出题上下文来自 PostgreSQL 中已落库的检索片段
（因此该模块不新增 `STORAGE_*` 依赖）。模型仍复用 `AI_BASE_URL` / `AI_MODEL` /
`AI_API_KEY` / `AI_TIMEOUT_SECONDS`；未配置时领取到的任务会以「模型服务未配置」
写入 `FAILED`，配置完成后可通过 `POST /jobs/{job_id}/retry` 重新生成。

**提交与批改（Grading）配套**：实验报告的 AI 批改由**第三个独立常驻进程**执行，
同样不在 Vercel 请求进程内调用模型：

```bash
python scripts/grading_worker.py
```

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SUBMISSION_GRADE_LEASE_SECONDS` | `300` | 批改任务租约（秒）；崩溃后超过租约的 `RUNNING` 任务可由重试接口回收 |
| `SUBMISSION_GRADE_MAX_CHARS` | `120000` | 送入模型的报告全文上限（字符），超出直接 `FAILED`，不截断后宣称成功 |
| `GRADING_WORKER_POLL_SECONDS` | `5` | 队列空时的轮询间隔 |
| `GRADING_WORKER_BATCH_SIZE` | `5` | 单批最多领取的任务数 |

提交与批改模块自身的其余配置：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SUBMISSION_MAX_UPLOAD_BYTES` | `52428800` | 实验报告单文件大小上限（50 MiB） |
| `SUBMISSION_UPLOAD_CONFIRM_TTL_SECONDS` | `86400` | 报告上传的完成确认窗口（24 小时） |
| `SUBMISSION_UPLOAD_DELETE_BUFFER_SECONDS` | `3600` | 报告上传对象清理缓冲（秒）：预签名 PUT 到期后再等该时长才删除孤立对象，覆盖晚到 PUT 的重建窗口 |

批改 Worker **需要对象存储**（从桶里下载报告原文并复核大小与 SHA-256，复用 `STORAGE_*`）
与模型配置（复用 `AI_BASE_URL` / `AI_MODEL` / `AI_API_KEY` / `AI_TIMEOUT_SECONDS`）；
模型未配置时领取到的任务会以「模型服务未配置」写入 `FAILED`，配置后可通过
`POST /submissions/{submission_id}/grade` 重新触发。过期或被替代的报告上传对象由
独立维护命令清理：`python scripts/cleanup_expired_submission_uploads.py`
（幂等，可周期运行；删除对象后会再次 HeadObject 复查，发现晚到 PUT 则本轮不标记过期）。

发布顺序：**先执行数据库迁移（`0010 → 0011_agent_runs → 0012_submissions_grading → 0013_jobs_contract`），再启动全部 Worker（解析、练习、批改与 Agent），最后开放前端入口**。
`0009_practice_sets`、`0010_assignments`、`0011_agent_runs` 与 `0012_submissions_grading` 都只新增表与原生枚举，
不与前序迁移冲突，可在同一发布中按序执行。

**实验任务（Assignments）配套**：`0010_assignments` 只新增 `assignments`、
`assignment_rubric_versions`、`assignment_rubric_items` 三张表与 `assignment_status`
原生枚举。该模块**不新增环境变量、不需要对象存储、也不需要额外 Worker**——
创建、修改、发布、关闭与查询都在 API 请求内同步完成（评分规则版本在写入事务内追加）。
迁移可回退（`downgrade` 逐个删除表、索引与外键，最后删除枚举）。

**提交与批改（Grading）配套**：`0012_submissions_grading` 只新增 `submissions`、
`submission_upload_sessions`、`grade_reviews`、`grade_items`、`submission_grade_attempts`
五张表与 `submission_status`、`submission_grade_attempt_status` 两个原生枚举；
`grade_items.rubric_item_id` 与 `submissions.rubric_version_id` 是指向历史评分版本的
**RESTRICT 外键**（删除版本会被拒绝，历史批改不能静默失联）。
迁移可回退（`downgrade` 按外键反序删表，最后删除枚举），
`backend/tests/integration/test_migrations.py` 覆盖 `0011_agent_runs → 0012 → 0011_agent_runs → 0012` 往返，
并断言提交唯一、批改唯一、评分项唯一与分数范围约束在库侧生效。

**异步任务（Jobs）配套**：`0013_jobs_contract` 不新增表，只在 `jobs` 上增加两条 CHECK 约束：
`progress BETWEEN 0 AND 100`，以及"任务类型与资源类型必须配对"（含三类公开任务与内部
`AGENT_RUN / AGENT_RUN`）。降级只删除这两条约束，不影响数据与枚举；
`backend/tests/integration/test_migrations.py` 覆盖 `0012 → 0013 → 0012 → 0013` 往返，
并断言进度越界（`-1` / `101`）与非法组合被数据库拒绝。该迁移不含数据回填，
升级前建议按惯例做一次库级快照。

开发、Preview 和 Production 使用独立配置。任何密钥都不能使用 `VITE_` 前缀，也不能提交到仓库。

### 数据库环境与责任分工

| 环境 | 数据库 | 配置及迁移执行负责人 |
| --- | --- | --- |
| Development | 每位后端开发者维护独立本地 PostgreSQL | 开发者配置本地连接并自行执行迁移 |
| Preview | 独立云 PostgreSQL，与 Production 隔离 | 测试与交付负责人创建数据库、配置 Vercel `DATABASE_URL` 并执行迁移 |
| Production | 独立云 PostgreSQL，与 Preview 隔离 | 测试与交付负责人创建数据库、配置 Vercel `DATABASE_URL` 并执行迁移 |

后端基础负责人维护数据库连接与迁移规范。迁移文件随代码版本管理；Preview 和 Production 的迁移作为独立发布步骤执行。FastAPI 启动过程不得自动建表或执行数据库迁移。

迁移命令（在 `backend` 目录下执行，连接串取自 `DATABASE_URL`）：

```powershell
..\.venv\Scripts\python.exe -m alembic upgrade head          # 升级到最新
..\.venv\Scripts\python.exe -m alembic downgrade -1          # 回退一步
..\.venv\Scripts\python.exe -m alembic upgrade head --sql     # 只渲染 SQL，不连库
```

`backend/tests/integration/test_migrations.py` 会校验「迁移执行后的库结构 == ORM 模型」；
它在专用的 `<名>_migration_check` 库上跑（会回滚整个 schema），PostgreSQL 不可达时自动跳过。

## 4. Vercel 适配要求

- FastAPI 暴露模块级 `app` 对象。
- 启动过程不自动建表，不执行迁移、大文件下载或耗时模型加载。
- 文件上传使用预签名地址直传对象存储。
- 长耗时任务不依赖普通请求保持连接。
- 不使用进程内队列、全局内存缓存或本地文件保存关键状态。
- 数据库连接池按 Serverless 环境配置，并优先使用支持连接代理的数据库地址。
- 外部调用设置连接和读取超时，失败可重试但避免无限重试。

## 5. 健康检查

- `GET /health/live`：进程可响应时返回 200，不访问外部服务。
- `GET /health/ready`：检查数据库和关键配置；不可用时返回 503。

健康检查不得调用大模型或执行真实文件上传。

## 6. 发布流程

1. Pull Request 创建 Preview 部署。
2. 执行后端单元、集成和契约测试。
3. 执行前端类型检查、测试和构建。
4. 在 Preview 环境执行主流程冒烟测试。
5. 合并主分支并部署 Production。
6. 数据库迁移作为独立受控步骤执行，不放在每个函数冷启动中。
7. 按 `docs/acceptance.md` 的演示脚本复验。

## 7. 可观测性最低要求

- 每个请求生成或透传 request ID。
- 记录接口、状态码、耗时和用户 ID，避免记录敏感正文。
- AI 调用记录供应商、模型、提示词版本、耗时、token 使用量和任务 ID。
- 解析和批改失败可以根据 request ID 与 job ID 定位。
- 前端向用户展示安全错误消息，不直接暴露异常堆栈。

## 8. 交付验证清单

### 8.1 接口契约导出

后端改动接口后，在 `backend` 目录下执行导出脚本（不访问数据库），把产物随 PR 提交：

```powershell
..\.venv\Scripts\python.exe scripts\export_openapi.py   # → contracts/openapi/openapi.json
npx openapi-typescript contracts/openapi/openapi.json -o contracts/generated/api-types.ts   # 前端执行
```

### 8.2 本地开发库与迁移（每位后端开发者各自维护）

开发者在本地 PostgreSQL 建自己的库，`backend/.env`（已被 `.gitignore` 忽略）中配置 `DATABASE_URL`：

```powershell
# 建库（幂等；读取 .env 中的库名，不需要把 PostgreSQL 的 bin 加入 PATH）
..\.venv\Scripts\python.exe scripts\create_local_database.py

# 执行迁移（在 backend 目录下）
..\.venv\Scripts\python.exe -m alembic upgrade head

# 一键验证「全新数据库迁移 + 回滚」：以 <名>_test 作为测试库、<名>_migration_check 作为
# 迁移校验库（两者都由测试夹具建删，受库名后缀守卫保护），不触碰原库数据
..\.venv\Scripts\python.exe scripts\verify_local_migration.py
```

`backend/.env` 建议只写 ASCII：部分工具（alembic、PowerShell 5.1）会用系统 locale 编码读取，中文注释在 zh-CN Windows 上会乱码。

**注意**：`/health/ready` 只验证配置齐备与数据库连通性（`SELECT 1`），**不检查迁移是否已执行**。未跑迁移时健康检查仍会通过，缺表错误会在第一个业务请求时暴露。因此迁移必须作为独立发布步骤执行，不能依赖健康检查来发现遗漏。

关于时区：时间列使用 `TIMESTAMP WITH TIME ZONE`，存的是绝对时刻，应用侧统一按 UTC 读写并输出 `...Z` 结尾的 ISO 8601 字符串，**不依赖数据库服务器的 `TimeZone` 设置**（已在 `TimeZone=Asia/Shanghai` 的本地实例上验证输出仍为 UTC）。

### 8.3 Preview 验证项

Preview 部署完成后逐项确认：

| 项目 | 预期结果 |
| --- | --- |
| 应用入口 | Vercel Python Function 加载 `app/main.py` 的模块级 `app`，部署日志无导入错误 |
| 启动行为 | 启动日志只有"应用启动"与配置告警；不出现建表、迁移、模型加载 |
| 环境变量 | Preview 使用独立的 `APP_ENV=preview`、`APP_SECRET_KEY`、`DATABASE_URL`、`FRONTEND_ORIGINS`，不与 Production 复用 |
| `GET /health/live` | 200；不依赖数据库，冷启动即可用 |
| `GET /health/ready` | 200（配置齐备且数据库可达）；缺少必需配置或数据库不可达时为 503，且响应体为统一 `error` 结构 |
| CORS | 仅放行 `FRONTEND_ORIGINS` 中的域名；Preview 前端域名通过 `FRONTEND_ORIGINS` 或 `FRONTEND_ORIGIN_REGEX` 显式加入 |
| Auth 主流程 | 注册 `201` → 登录 `200`（返回两个 Token）→ `/users/me` `200` → 刷新 `200`（只返回新 Access Token）→ 注销 `204` → 原 Refresh Token 再刷新 `401` |
| 迁移 | Preview 与 Production 的迁移在各自环境单独执行，不随函数冷启动 |

上表可以用脚本自动跑一遍（会对目标环境真实注册一个临时账号，请勿指向 Production 正式数据）：

```powershell
# 本地或 Preview
..\.venv\Scripts\python.exe scripts\verify_deployment.py https://<preview-domain> https://<允许的前端来源>
```

脚本输出逐项 PASS/FAIL 与通过率，任一失败返回非零码，可直接接进 CI。

### 8.4 Production 发布

1. 合并主分支后先在 Preview 完成 8.3 全部项目。
2. 由测试与交付负责人在 Production 数据库上单独执行 `alembic upgrade head`。
3. 部署 Production，复查 `/health/live` 与 `/health/ready`。
4. 按 `docs/acceptance.md` 第 11 节演示脚本复验。
