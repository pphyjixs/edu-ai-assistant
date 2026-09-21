# 第一版本验收标准

## 1. 总体验收定义

版本通过验收需要同时满足：

- 教师和学生能在部署环境完成完整主流程。
- 所有越权请求由后端拒绝，不能只在前端隐藏按钮。
- 数据库、对象存储和 AI 服务可通过环境变量切换。
- 页面刷新后关键业务状态不丢失。
- AI 失败时有明确状态、错误提示和安全重试方式。
- 核心 API 有集成测试，核心页面有交互测试。
- README、接口文档和实际实现一致。

## 2. Auth 验收

- [x] 任何人均可注册教师或学生，无需教师审批或邮箱验证，注册后可通过邮箱和密码登录。（`POST /auth/register` 无审批/邮箱验证分支，`is_active` 默认启用；`integration/test_auth_flow.py` 注册 201 后立即登录 200）
- [x] 注册邮箱重复返回 409；两个并发注册请求只会成功一个，不会出现比较值相同的两条账号记录。（注册不做“先查后插”，`uq_users_email_normalized` 唯一约束兜底，`IntegrityError` → 409 `AUTH_EMAIL_TAKEN`；`integration/test_concurrent_registration.py` 8 路真并发断言恰好 1×201 + 7×409 且库中仅 1 行）
- [x] 同一规范化邮箱连续登录失败达到阈值后返回 429 并给出可重试秒数，登录成功后计数清零。（默认 15 分钟窗口 5 次阈值，`retry_after_seconds` 为最早失败滚出窗口所需秒数；限流按规范化邮箱隔离且对不存在账号同样生效，成功登录物理删除失败记录；`integration/test_auth_login_security.py`。轻微测试缺口：窗口滚过后限流解除未直接断言，由 `retry_after_seconds` 计算逻辑保证）
- [x] 邮箱域名大小写不同视为重复账号，本地部分大小写不同视为不同账号；并发注册不能绕过数据库唯一约束。（`normalize_email` 仅小写化域名，本地部分原样保留且不移除句点与 `+` 后缀；`integration/test_auth_acceptance.py` 直查库断言 `email_normalized`，`integration/test_concurrent_registration.py` 含 4 种域名大小写变体并发）
- [x] 密码允许 8–128 个字符及空格，不强制字符种类组合；少于 8 或多于 128 个字符被拒绝，不自动去除密码首尾空格。（`RegisterRequest` 密码 `min_length=8 / max_length=128` 且不 strip，纯小写字母密码可注册；`integration/test_auth_acceptance.py` 边界 8/128 通过、7/129 返回 422，首尾空格差异登录返回 401）
- [x] 正确凭证可以登录，错误凭证不会泄露账号是否存在。（账号不存在/密码错误/停用统一 401 `AUTH_INVALID_CREDENTIALS`，账号不存在时用哑哈希补齐 Argon2 计算量防计时侧信道；`integration/test_auth_login_security.py` 断言两种错误状态码、错误码与文案一致）
- [x] Access Token 有效期为 1 小时；不携带有效 Access Token 也可通过 JSON 请求体提交 Refresh Token 刷新。（JWT HS256，登录响应 `expires_in == 3600`；`/auth/refresh` 无 Bearer 依赖、仅 JSON Body 携带 `refresh_token`；`contract/test_token_contract.py`、`integration/test_auth_acceptance.py` 以 2 秒 TTL 真实等待验证到期失效）
- [x] Refresh Token 自登录签发起有效 7 天，可重复刷新且不轮换、不延长有效期；刷新响应只返回新的 Access Token 及其类型和有效期。（`expires_at = 登录时间 + 7 天`，刷新路径不写会话表即不轮换不延长；`RefreshResponse` 仅 `access_token`/`token_type`/`expires_in` 三字段；`integration/test_auth_flow.py` 同一 Refresh Token 二次刷新成功且直查库断言 7 天，`contract/test_token_contract.py` 断言刷新响应无 `refresh_token` 字段）
- [ ] 刷新失败后清除前端保存的两个 Token 并回到登录页；验收不限定前端 Token 存放位置或内部键名。（后端已验收：无效/过期/已撤销的 Refresh Token 统一返回 401；`services/http.ts` 已具备清 Token 与跳转回调基础设施。**前端尚未接入**：`features/auth/api` 仍为 mock，无登录页可跳转）
- [x] 注销仅撤销当前用户指定的 Refresh Token 会话，不影响其他会话；已撤销的 Refresh Token 再次刷新返回 401。（按令牌哈希定位会话，撤销他人会话返回 404 防枚举，仅置 `revoked_at` 幂等；`integration/test_auth_login_security.py` 多会话独立撤销，`integration/test_auth_acceptance.py` 注销后再刷新 401）
- [ ] 注销后前端清除其保存的两个 Token；单独保存的旧 Access Token 仍可能使用至自身到期。（后端已验收：注销不维护 Access Token 黑名单，旧 Access Token 在自身 `exp` 前仍可访问 `/users/me`，到期后失效。**前端清 Token 尚未接入**：前端无注销功能调用 `/auth/logout`）
- [ ] 未登录访问受保护页面时跳转登录页。（后端已验收：缺失/格式错误/错误 scheme 的 Bearer 统一 401 `AUTH_TOKEN_EXPIRED`，未认证先于角色判断。**前端尚未接入**：`router.tsx` 无路由守卫与 `/login` 路由）
- [x] 学生调用教师接口返回 403。（`require_roles` 抛 403 `ROLE_FORBIDDEN`，匿名访问教师接口先返回 401；`integration/test_auth_acceptance.py` 经 `TeacherDep` 探针路由验证守卫本身，课程创建与资料上传初始化/完成等真实业务端点均已接入 `TeacherDep` 并有学生 403 用例：`integration/test_courses_api.py`、`integration/test_materials_upload_api.py`）
- [x] 密码只保存 Argon2id 哈希，Refresh Token 只保存哈希；日志不记录密码或令牌。（密码 Argon2id 默认参数哈希，Refresh Token 为 192-bit 随机值仅存 sha256；`integration/test_auth_flow.py` 直查库断言哈希形态与明文不落库，`integration/test_log_redaction.py` 真实请求链路日志无密码/令牌，`unit/test_logging_redaction.py` 覆盖出口兜底脱敏规则）

## 3. Courses 验收

- [x] 教师创建课程返回 `201 CourseDetailWithInviteCode`，状态为 `ACTIVE`，`teacher_id` 指向本人，且自动建立教师成员记录；学生创建课程返回 `403 ROLE_FORBIDDEN`。
- [x] 课程名称去除首尾空白后须为 1–100 字符，说明最多 2000 字符；创建时省略说明返回空字符串。超限、全空白名称、未知字段和显式 `null` 返回 `422 VALIDATION_ERROR`。
- [x] 修改课程允许只提交名称或说明；省略字段保持原值，空字符串说明清空原值，空对象被拒绝；响应为更新后的 `CourseDetailWithInviteCode`。
- [x] 课程列表分页返回本人参与的活动与归档课程，成员列表也按 `items/page/page_size/total` 分页；默认每页 20 条，最大 100 条。
- [x] 学生用当前有效邀请码首次加入活动课程返回 `201 CourseSummary`，重复加入返回 `200 CourseSummary`；并发重复加入不会产生重复成员记录。
- [x] 无效或已被重置的邀请码返回 `422 INVITE_CODE_INVALID`；教师调用加入接口返回 `403 ROLE_FORBIDDEN`。
- [x] 非课程成员读取详情返回 `403 COURSE_FORBIDDEN`；其他教师不能管理课程或读取成员列表；不存在的课程返回 `404 RESOURCE_NOT_FOUND`。
- [x] 创建教师可查看成员列表，列表包含教师和学生的 `user_id`、`display_name`、`course_role`、`joined_at`，不返回邮箱。
- [x] 创建教师可重置邀请码，返回 `200` 与新码，旧码立即失效；当前邀请码仅出现在其未归档课程详情或重置响应中。
- [x] 课程列表、学生响应和已归档课程详情均省略 `invite_code`，不返回 `null`；课程概要、详情及时间格式与 API 契约一致。
- [x] 归档成功返回 `200 CourseDetail` 和 `ARCHIVED` 状态；重复归档返回当前详情；成员仍能读取课程。
- [x] 已归档课程的修改、加入及邀请码重置返回 `409 COURSE_ARCHIVED`，已加入学生再次加入也不例外；无权限者不能通过这些接口读取归档状态。
- [ ] 归档课程停止新增资料、任务、提交和发布任务；第一版不提供恢复接口。资料、任务、提交和发布接口尚未实现，待后续模块接入 `require_course_active` 后验收。
- [x] 所有课程错误使用统一错误结构，响应头与错误体的 request ID 一致；OpenAPI 声明各接口的响应 Schema、成功状态码及业务错误。

## 4. Materials 验收

- [x] 教师可以上传 PDF、PPTX、DOCX，学生不能上传课程资料。（初始化与完成接口已实现并验收：学生 `403 ROLE_FORBIDDEN`，其他教师 `403 COURSE_FORBIDDEN`）
- [x] 文件直接进入对象存储，不依赖后端本地磁盘持久化。（预签名直传：后端只签发地址与读取对象元数据，从不接收文件内容）
- [x] 不支持的类型、超限文件和校验失败有明确错误。（业务规则为 `422 UPLOAD_INVALID` + 稳定 `details.reason`，结构问题为 `422 VALIDATION_ERROR`）
- [ ] 上传后显示处理状态和进度。（后端 `GET /materials/{material_id}` 与 `GET /jobs/{job_id}` 已验收：`PROCESSING`/`PENDING` 表示排队或处理中，状态由解析 Worker 推进；前端展示尚未验收。课程成员可读，非成员与不存在统一 `404 RESOURCE_NOT_FOUND`，归档课程仍可读）
- [ ] 成功解析后显示有顺序的大纲和知识点。（**后端已交付**：解析 Worker 推进 `READY` 后，`GET /materials/{material_id}/outline` 返回按 `order` 排列的章节与知识点，课程成员可读、未就绪 `409 MATERIAL_NOT_READY`、失败 `502 AI_JOB_FAILED`；前端展示尚未验收）
- [ ] 解析结果保留来源页码或章节定位。（**后端已交付**：章节与知识点均含 `source_type` 与从 1 开始的 `location_start`/`location_end`（PDF 页码 / PPTX 幻灯片号 / DOCX 段落序号），知识点含可核对原文摘录 `quote`；前端展示尚未验收）
- [ ] 解析失败可以重试，不产生重复章节和知识点。（**后端已交付**：失败资料 `502 AI_JOB_FAILED` 带 `details.job_id`，`POST /materials/{material_id}/parse` 复用原 job ID 重置为 `PENDING` 并全量重写解析产物；已就绪资料返回 `409 MATERIAL_ALREADY_READY`；前端重试入口尚未验收）

## 5. Learning 验收

- [x] 学生只能在自己加入的课程中创建问答会话。（`integration/test_chat_api.py`：非成员创建会话与列表统一 `404 RESOURCE_NOT_FOUND`，同课程其他成员不是所有者时读写消息同样 `404`，不泄露会话存在性）
- [x] 回答基于该课程已就绪资料，不跨课程泄漏内容。（检索 SQL 限定课程 ID、资料未删除且 `READY`；`integration/test_chat_scope.py` 断言提示词只含本课程 `READY` 资料的片段——跨课程、`PROCESSING` 与已删除资料的原文都不出现）
- [ ] 有依据的回答展示至少一个可点击引用。（**后端已交付**：`grounded=true` 时引用含资料 ID/名称、命中章节（无法匹配为 `null`）、来源类型、从 1 开始的位置与已校验的原文摘录；**前端引用跳转尚未接入**）
- [ ] 资料不足时回答明确提示“课程资料中未找到依据”。（**后端已交付**：无 `READY` 资料或检索不到内容、以及模型引用全部校验失败时，固定返回该文案 + `grounded=false` + 空引用，仍是 `201`；**前端展示尚未验收**）
- [ ] 教师可生成包含指定数量和题型的练习。（**练习模块尚未交付**，第一版只交付后端问答能力）
- [ ] 学生提交练习后看到得分、答案和解析。（**练习模块尚未交付**）
- [ ] 练习和问答在刷新页面后仍可查看。（问答**后端已交付**：会话、消息、引用与生成尝试持久化，`integration/test_chat_api.py` 断言重新查询后一问一答与引用完整一致；练习尚未交付）

## 6. Assignments 验收

- [ ] 教师可创建包含多个评分项的实验任务。
- [ ] 评分项分值合计不等于总分时不能保存或发布。
- [ ] 草稿对学生不可见，发布后学生可以查看。
- [ ] 截止时间和是否允许补交按规则执行。
- [ ] 任务状态变化后，前后端显示一致。
- [ ] 评分规则修改记录版本，已有批改结果不被静默覆盖。

## 7. Submission 与 Grading 验收

- [ ] 学生可上传并提交一份实验报告，提交记录关联本人和任务。
- [ ] 学生无法读取其他学生的报告和成绩。
- [ ] 教师能查看课程内所有提交和当前状态。
- [ ] AI 按每个评分项返回建议得分、解释、证据定位和改进建议。
- [ ] AI 各项建议分数不超过对应满分。
- [ ] 教师可以修改每项得分和评语。
- [ ] 未经教师复核的结果不能发布。
- [ ] 发布后学生看到教师最终结果，而不是未确认的 AI 草稿。
- [ ] 系统保留 AI 原始建议与教师最终结果的差异。
- [ ] 批改失败可以重试，重试不会生成多个有效终稿。

## 8. Jobs 验收

- [x] 创建异步操作后 API 在合理时间内返回 202 和任务 ID。（完成上传返回 `202` 与 `MATERIAL_PARSE` 任务；`GET /jobs/{job_id}` 查询接口已实现，可见性等同于资料所属课程成员）
- [x] 任务状态只能按合法路径变化，例如 `PENDING → RUNNING → SUCCEEDED`。（解析 Worker 已交付并验收：完成上传后原子领取任务推进 `PENDING → RUNNING`，解析成功落库章节/知识点后任务 `SUCCEEDED`（progress 100）、资料 `READY`；失败路径任务与资料同步 `FAILED`，重试解析复用原 job ID 重置回 `PENDING`）
- [x] 任务包含 0 到 100 的进度或明确的不确定进度状态。（`PENDING`/`RUNNING` 起始进度为 0，`SUCCEEDED` 为 100，符合契约）
- [x] 同一幂等请求不会并行创建重复任务。（`(type, resource_id)` 唯一约束 + 完成接口行锁 + Worker 原子领取，重复/并发完成与重试只产生一个任务）
- [ ] 失败任务包含面向用户的错误说明和内部 request ID。（**后端已交付**：`FAILED` 任务与资料写入可安全展示的中文摘要，不回显堆栈或原始内容，错误结构含 request ID；前端展示尚未验收）
- [ ] 前端停止轮询已完成、失败或取消的任务。（后端任务状态已由解析 Worker 推进到 `SUCCEEDED`/`FAILED` 终态，前端轮询逻辑尚未实现，属前端验收范畴）

## 9. 前端体验验收

- [ ] 教师和学生登录后进入各自工作台。
- [ ] 所有数据页面具有加载、空数据、错误和成功状态。
- [ ] 上传和 AI 任务具有可见进度，不出现无反馈等待。
- [ ] 桌面端主流程可用，宽度 375px 的移动端无水平溢出。
- [ ] 主要交互可用键盘完成，表单字段有关联标签和错误提示。
- [ ] 颜色不是表达状态的唯一方式，正文和按钮对比清晰。
- [ ] 页面级异常不会导致整个应用白屏。

## 10. 后端质量验收

- [ ] OpenAPI 可以正常生成并覆盖本文档中的公开接口。
- [ ] Pydantic 校验错误转换为统一错误结构。
- [ ] 数据库变更通过迁移脚本完成。
- [ ] 权限测试至少覆盖匿名、错误角色、非成员和资源所有者。
- [ ] 外部 AI、存储服务通过接口封装，可在测试中替换为 fake。
- [ ] 日志包含 request ID，不包含密码、令牌或完整报告正文。
- [ ] `/health/live` 与 `/health/ready` 返回正确状态。

## 11. 演示验收脚本

比赛演示前必须连续通过以下流程：

1. 教师登录并创建课程。
2. 教师上传一份课件，等待大纲和知识点生成。
3. 学生通过邀请码加入并查看课件大纲。
4. 学生向课程助手提问，打开回答引用。
5. 教师生成并发布一组练习，学生完成并查看解析。
6. 教师创建带三个评分项的实验任务并发布。
7. 学生上传实验报告。
8. 教师触发 AI 批改，查看逐项建议和原文证据。
9. 教师修改一项分数和评语后发布。
10. 学生查看最终成绩、分项反馈和修改建议。

以上流程应在全新测试账号中完成，不依赖手工修改数据库。

## 12. 自动化验收测试

```powershell
cd backend
..\.venv\Scripts\python.exe -m pytest -q            # 全部
..\.venv\Scripts\python.exe -m pytest tests/unit -q          # 不依赖数据库
..\.venv\Scripts\python.exe -m pytest tests/contract -q      # 契约（需数据库）
..\.venv\Scripts\python.exe -m pytest tests/integration -q   # 集成（需数据库）
```

分层与数据库使用方式：

| 层 | 数量 | 数据库 |
| --- | --- | --- |
| `tests/unit` | 167 | 不接触数据库与网络；外部依赖替换为 fake 或 `MockTransport` |
| `tests/integration` | 224 | **专用测试库**（表结构由 Alembic 迁移创建）；对象存储与解析 Worker 端到端用例需配置 S3 端点 |
| `tests/contract` | 54 | 同上（令牌格式、课程、课件上传与课程问答契约、OpenAPI 一致性） |
| 合计 | 445 | 连真实 PostgreSQL + 对象存储时除 1 项契约允许分支外全部通过（模型为本地假 HTTP 服务） |

测试库规则（见 `backend/tests/pg_support.py`）：

1. **只设置 `TEST_DATABASE_URL` 就能跑**，不需要同时配置 `DATABASE_URL`。
   未设置 `TEST_DATABASE_URL` 时，由 `DATABASE_URL` 的库名派生 `<库名>_test`。
2. **只允许 `_test` 结尾的专用库**。库名不合法、不是 `_test` 结尾、
   与开发库或系统库（`postgres` / `template0` / `template1`）同名，
   一律直接报错中止，不会去动任何非测试库。
3. **创建前检查冲突**。目标库若已存在：还有别的连接在用就报错中止
   （避免打断别人正在跑的测试），否则视为上次异常退出的残留并重建。
4. **结束后清理**。集成测试库与迁移库在会话结束时都会被删除，
   运行前后 `pg_database` 里不应残留。只有本次成功创建的库才进入清理流程；
   创建被拒绝时保留目标库，建库成功后迁移失败仍会清理。
5. 建库后由子进程执行 `alembic upgrade head` 建表（就是文档里写的那条命令），
   不使用 `metadata.create_all`。

迁移专项校验（`tests/integration/test_migrations.py`）另用 `<名>_migration_check`，
因为它会回滚整个 schema；该名字同样受后缀守卫保护。

需要数据库的用例在以下条件下跳过（单元测试及不依赖数据库的日志用例仍会执行）：

- `TEST_DATABASE_URL` 与 `DATABASE_URL` 都没有配置，或连接串不是 PostgreSQL；
- PostgreSQL 实例不可达；
- `tests/integration/test_storage_minio.py` 需要 `TEST_S3_ENDPOINT` / `TEST_S3_BUCKET` /
  `TEST_S3_ACCESS_KEY` / `TEST_S3_SECRET_KEY`，未配置或端点不可达时整组跳过。

跳过信息会写清原因（例如 `未配置可用的测试数据库（...）`）。
**提交前请确认没有因数据库或对象存储缺失而跳过的用例**；存储实现不回显可选校验头时，相关用例按“未验证”单独说明。只跑单元测试通过不代表验收通过。

覆盖对照（关键条目 → 用例）：

| 验收条目 | 用例位置 |
| --- | --- |
| 邮箱域名/本地部分大小写、句点与 `+` 后缀 | `integration/test_auth_acceptance.py` |
| 并发注册唯一约束 | `integration/test_concurrent_registration.py` |
| 密码 8–128 边界、空格与大小写敏感 | `integration/test_auth_acceptance.py` |
| 错误凭证一致性与登录限流 | `integration/test_auth_login_security.py` |
| 过期 Access Token、过期/撤销的 Refresh Token | `integration/test_auth_acceptance.py` |
| 多会话独立注销、注销后旧 Access Token 行为 | `integration/test_auth_login_security.py` |
| 401/403/404/422/429 统一错误结构 | `integration/test_auth_acceptance.py` |
| 日志脱敏 | `integration/test_log_redaction.py`、`unit/test_logging_redaction.py` |
| 健康检查 | `integration/test_health_acceptance.py`、`unit/test_health.py` |
| OpenAPI 与导出物一致性 | `contract/test_openapi_contract.py` |
| 令牌请求/响应格式（不检查前端存储位置） | `contract/test_token_contract.py` |
| 启动不建表、不迁移、不连库 | `unit/test_startup_contract.py` |
| 测试库只允许 `_test` 专用库、创建前冲突检查、结束清理 | `unit/test_pg_support.py`、`integration/test_test_database_lifecycle.py` |
| 建库被拒绝时保留目标库、迁移失败后清理本次创建的库 | `unit/test_database_fixture_cleanup.py` |
| 课程 8 个接口、分页、邀请码轮换、归档后读写 | `integration/test_courses_api.py` |
| 首次 201 / 重复 200 加入、并发成员唯一、并发重置邀请码 | `integration/test_courses_api.py` |
| 归档状态不泄露给无权限者、成员列表不含邮箱、默认每页 20 | `integration/test_courses_api.py` |
| 课程请求校验、邀请码生成、教师/成员/归档权限判断 | `unit/test_courses.py` |
| 课程成功状态码、错误结构、邀请码字段可见性、Bearer 要求 | `contract/test_courses_contract.py` |
| 新增表、约束、枚举与升级回滚 | `integration/test_migrations.py` |
| 上传会话、资料与解析任务在“重启”后仍可读取 | `integration/test_materials_persistence.py` |
| 一次上传只对应一条资料、一个 `MATERIAL_PARSE` 任务 | `integration/test_materials_persistence.py` |
| SigV4 签名向量、预签名头、对象键不含用户文件名、存储故障 → 503 | `unit/test_storage_signing.py` |
| MinIO 直传成功、内容与签名哈希不符被拒、重复 PUT 被拒、跨域预检、后端不代传文件 | `integration/test_storage_minio.py` |
| 初始化：创建教师 201、学生/其他教师/归档/非法类型/超限/结构错误、失败不落库 | `integration/test_materials_upload_api.py` |
| 完成：未上传/元数据被篡改/过期会话/上传后归档/重复与并发完成，一上传一资料一任务 | `integration/test_materials_upload_api.py` |
| 课件上传接口的路径、状态码、Bearer、响应组件与枚举、组件名稳定 | `contract/test_materials_contract.py` |
| 资料详情与任务状态查询：成员可读、非成员/不存在统一 404、归档可读 | `integration/test_materials_query_api.py` |
| 资料/任务状态查询接口的路径、Bearer、响应组件（`MaterialDetail` / `JobStatus`） | `contract/test_materials_contract.py` |
| 资料列表：成员可读、分页与倒序、归档可读、不含已删除、非成员 404 | `integration/test_materials_api.py` |
| 删除：创建教师 204/幂等 204、学生 403、归档 409、删除后读路径与对象清理 | `integration/test_materials_api.py` |
| 重试解析：失败 202 复用 job ID、就绪 409、处理中幂等 202、归档 409 | `integration/test_materials_api.py` |
| 大纲查询：READY 200 含定位与摘录、PROCESSING 409、FAILED 502 带 job_id | `integration/test_materials_api.py` |
| 解析 Worker：`PENDING → RUNNING → SUCCEEDED`/`FAILED`、产物落库、失败不落库 | `integration/test_materials_api.py` |
| 解析器：DOCX/PPTX 章节与知识点抽取、损坏与空文本失败、来源类型推导 | `unit/test_materials_parser.py` |
| 删除流水线：删除事务取消解析并写待办、维护命令延迟删除、晚到 PUT 核查、存储故障重试 | `integration/test_materials_api.py` |
| 完成响应快照：重复确认（含资料删除/状态变化后）返回首次结果 | `integration/test_materials_api.py` |
| Worker 模型分支：无效 JSON、HTTP 500、幻觉摘录、超时、AI 未配置 → FAILED | `integration/test_materials_api.py` |
| 解析上限：全文超 120,000 字符直接 FAILED，不截断 | `integration/test_materials_api.py` |
| 扫描版 PDF：无可提取文本明确 FAILED，不做 OCR | `integration/test_materials_api.py` |
| 崩溃恢复：RUNNING 租约过期后重试解析回收任务 | `integration/test_materials_api.py` |
| 竞态回归：重试撤销旧令牌后，旧执行者成功/失败回写均被拒绝，新执行者正常完成 | `integration/test_materials_api.py` |
| 解析中删除：回写前检查删除标记与运行令牌，放弃发布、任务保持 CANCELLED | `integration/test_materials_api.py` |
| 真实 MinIO 端到端：DOCX/PPTX/PDF 直传→解析→READY，删除后真实对象清理 | `integration/test_parse_worker_minio.py` |
| 列表/删除/重试/大纲的路径、状态码、响应组件（`MaterialOutline` 等）与错误码 | `contract/test_materials_contract.py` |
| 过期上传清理：删孤立对象并标记过期、幂等、已完成资料不删、与完成请求争锁、存储不可用跳过；维护命令连接隔离测试库与真实测试桶执行两次 | `integration/test_upload_cleanup.py` |
| 上传会话 `expired_at` 过期清理标记列 | `integration/test_migrations.py` |
| 问答检索与引用校验、模型异常分支（离线） | `unit/test_chat_retrieval_and_answer.py` |
| 问答四接口的路径、状态码、Bearer、响应组件与错误码枚举 | `contract/test_chat_contract.py` |
| 会话与消息权限、隔离、持久化、请求校验 | `integration/test_chat_api.py` |
| 跨课程/未就绪/已删除不进提示词、归档竞态、并发 409、模型失败 502/503 | `integration/test_chat_scope.py` |
| 片段回填：幂等、force 重写、提交前复查、失败安全摘要 | `integration/test_materials_backfill.py` |
| `pg_trgm` 扩展、chat 四表、片段 GIN 索引的升级与回退 | `integration/test_migrations.py` |

本次查询与清理交付验证：334 项（128 unit / 165 integration / 41 contract），
连真实 PostgreSQL + MinIO（Quay 镜像 `quay.io/minio/minio`，本机以 Docker 启动，
`MINIO_API_CORS_ALLOW_ORIGIN` 配置前端来源）时 334 通过、0 跳过。
该次完整运行使用独立的 `edu_ai_material_upload_test` 测试库，结束后测试库及其迁移、
生命周期检查库均无残留。此前一次使用共享默认测试库的重跑在最后 4 项遇到
数据库连接中断；独立测试库重跑后这 4 项及全套均通过。

本次 Auth 验收核对运行：本地 PostgreSQL（未配置 `TEST_S3_*`）下 334 项全部收集，
325 通过、9 跳过（8 项 `test_storage_minio.py` 与 1 项 `test_upload_cleanup.py` 的
真实存储删除用例，原因均为 `未配置对象存储测试端点`，与 Auth 无关）、0 失败；
unit 128 / contract 41 / integration 165，Auth 相关用例全部真实执行且通过。
运行前清理了上次异常退出残留的 `edu_ai_dev_test` 与 `edu_ai_dev_migration_check`
（无活动连接，符合规则 3 的残留判定），本次会话结束后复查 `pg_database` 无残留。

资料接口与解析 Worker 交付验证：独立测试库 `edu_ai_pr_verify_test` + MinIO
测试桶 `edu-ai-test` 下 387 项全部通过、0 失败、0 跳过
（unit 141 / contract 47 / integration 199）；资料列表、删除、重试解析、
大纲查询及 Worker 状态推进用例均真实执行，运行后 `pg_database` 无残留。
删除流水线专项：删除事务取消未完成解析并写入对象删除待办；维护命令在
PUT 地址过期 + 缓冲期后删除对象、失败持续重试、删除后核查晚到 PUT；
存储故障期间待办保留并在恢复后重试成功；完成响应快照保证重复确认
（含资料删除后）返回首次结果；迁移 0006 升级/回退均通过。
Worker 端到端（`test_parse_worker_minio.py`，真实 MinIO + 本地假模型
HTTP 服务）：DOCX / PPTX / PDF 三种格式经真实预签名直传后由 Worker
从真实桶流式读取（复核大小与 SHA-256）解析为 READY；删除后维护命令
从真实桶移除对象。**真实外部模型调用未验收**：假模型服务仅覆盖
Chat Completions 请求形状与失败分支，模型生成质量属后续验收。

可检索原文片段（契约 6.1 / 迁移 `0007_material_chunks`）交付验证：
402 项（152 unit / 203 integration / 47 contract），连真实 PostgreSQL 时
**389 通过、13 跳过、0 失败**；13 项跳过全部是未配置 `TEST_S3_*` 的对象存储
用例（`test_storage_minio.py` 8 + `test_parse_worker_minio.py` 4）与
`test_upload_cleanup.py` 的真实存储删除 1 项。本步覆盖：

- 切片规则（`unit/test_materials_retrieval_chunks.py`）：片段约 1,000 字符、
  相邻片段重叠 100±10 字符（按原文位置度量）、顺序号从 1 连续、
  单个超长来源单元同样切成多片、参数非法直接拒绝；
- 落库（`integration/test_materials_api.py`）：解析成功时片段与章节、知识点、
  资料 `READY`、任务 `SUCCEEDED` 在同一事务内先清后写，正文句子可检索；
- 不产生重复或半份片段：队列空后再驱动 Worker 不新增；强制把任务重置为
  `PENDING` 再解析，片段内容与数量完全一致（全量重写）；
- 解析失败不落库任何片段；**旧执行者回写**（`test_retry_revokes_stale_worker_write_back`
  竞态回归用例已扩展）连同片段一起被拒绝，片段数为 0；
- 删除资料后片段被清空，不再被检索。

HeadObject 带 `x-amz-checksum-mode: ENABLED` 后，MinIO 回显已存储的
`x-amz-checksum-sha256`，校验值读取也由真实存储用例验证。真实 PUT、内容与签名哈希不符被拒、
重复 PUT 被拒、篡改签名头被拒、跨域预检、后端不代传文件内容等用例在 MinIO 上均完整执行并断言。

**解析 Worker 已交付**（契约 5.5）：完成上传与重试解析后由后台任务执行解析，
推进资料 `PROCESSING → READY/FAILED` 与任务 `PENDING → RUNNING → SUCCEEDED/FAILED`；
大纲/知识点查询（`GET /materials/{material_id}/outline`）、失败重试
（`POST /materials/{material_id}/parse`）、资料列表与删除接口均已交付并验收。
第一版不做扫描版 PDF 的 OCR，也不提供通用 `POST /jobs/{job_id}/retry`；
前端对解析状态、大纲与重试入口的展示仍属后续阶段。

对象存储验收怎么跑（`tests/integration/test_storage_minio.py`，用例对服务端不做假设）:

```powershell
cd backend
..\.venv\Scripts\python.exe scripts\verify_storage.py `
    --endpoint http://127.0.0.1:9000 --bucket edu-ai-test `
    --access-key <access> --secret-key <secret>
```

脚本会按需创建测试桶、运行用例并返回退出码。**不是所有 S3 兼容实现都强制校验
签名、校验头与有效期**：遇到不实现该行为的服务端，相关用例会带原因 skip
（例如 `该 S3 实现未校验 SigV4 签名，需在 MinIO/AWS S3 上验证本项`），
按"未验证"而不是"通过"对待；本次 MinIO 验收没有此类跳过。
本地使用 Docker 镜像 `quay.io/minio/minio` 和专用测试桶；
`MINIO_API_CORS_ALLOW_ORIGIN` 或桶级 CORS 规则须包含测试来源（默认 `http://localhost:5173`，可由 `TEST_S3_CORS_ORIGIN` 覆盖）。
在本地 PostgreSQL 上执行完整套件，运行后确认测试库、迁移库及生命周期检查库均无残留。
存在 2 条测试客户端依赖弃用警告。
Preview 尚未验证；其他教师不能管理他人课程的判断已由课程模块覆盖，
平台角色守卫（`ROLE_FORBIDDEN`）已由课程创建、资料上传/删除、重试解析等
真实业务端点验证（`integration/test_courses_api.py`、`integration/test_materials_api.py`），
并保留 `TeacherDep` 探针用例覆盖守卫本身。

契约测试只校验请求与响应格式：契约 2.2 把令牌存放位置交给前端自行决定，
因此测试反过来断言服务端不通过 Cookie 下发令牌，不假设也不约束前端的存储方式。

课程问答（契约第 6 节 / 迁移 `0008_chat_qa`）交付验证，独立测试库
`edu_ai_chat_verify_test` + MinIO 测试桶 `edu-ai-test`：**445 项收集，
444 通过、1 跳过、0 失败**（unit 167 / contract 54 / integration 224）。
唯一跳过是 `integration/test_storage_minio.py:289` 的契约允许分支
（S3 实现未校验 `X-Amz-Expires`，该分支不适用），**不是**因缺少数据库或
对象存储依赖而跳过；PostgreSQL 与对象存储相关用例全部真实执行。
运行后复查 `pg_database`，测试库与迁移检查库均无残留。

本步覆盖：

- 检索与引用校验（`unit/test_chat_retrieval_and_answer.py`）：关键词构造
  （ASCII 词 + 中文 2-gram、去重与上限）、提示词只含本次检索片段、
  引用片段 ID 越界或摘录不在原文即丢弃、全部无效时降级为无依据、
  模型未配置/输出非法/HTTP 错误/超时的异常分支；
- 四接口契约（`contract/test_chat_contract.py`）：路径、成功状态码、
  Bearer、`ChatSessionSchema` / `ChatMessageSchema` / `Citation` 字段集、
  请求体只含 `content`、错误码枚举含 `CHAT_CONFLICT` 与 `SERVICE_UNAVAILABLE`；
- 权限与隔离（`integration/test_chat_api.py`）：非成员与匿名、非所有者
  统一 `404`，会话列表各看各的、归档后列表仍可读但创建/提问 `409`；
- 检索边界（`integration/test_chat_scope.py`）：跨课程、`PROCESSING` 与
  已删除资料的原文都不进入提示词；删除资料后回答转为无依据且不再调用模型；
- 受约束生成：有依据时 `grounded=true` 且引用可回查（片段原文中确实存在该摘录）、
  无依据时固定文案 + 空引用且仍是 `201`；刷新后对话与引用完整一致；
- 并发与失败：并发提问恰好一个 `201`、一个 `409 CHAT_CONFLICT` 且只写入
  一问一答（HTTP 层用两个客户端并发，断言"成功次数 × 2 == 消息数"以排除半组消息；
  另有 service 层确定性用例，两个协程同时发送必得一个冲突、消息仍只有一组）；
  模型超时 / 5xx / 无效输出 → `502`、未配置 → `503`，都不写消息、
  只留一条安全尝试记录（不含提示词、原文或模型地址）；
- 片段回填（`integration/test_materials_backfill.py`）：缺片段的 `READY`
  资料被补建、重复执行结果一致、`--force` 全量重写不累积、
  提交前复查拦截回填期间被删除的资料、对象缺失时输出安全摘要并保留原状态。

**模型侧验收边界**：以上问答用例全部使用本地假模型 HTTP 服务
（`httpx.MockTransport`），覆盖请求形状、引用校验与失败分支；
**真实外部模型联调尚未验收**（当前环境未配置可用的 Chat Completions
端点与密钥）。真实模型效果（回答质量、语言一致性）需在配置完成后
单独验收，不以模拟服务通过代替。

**前端**：聊天页面与引用跳转尚未接入，第 5 节 Learning 中涉及前端展示的
条目保持未勾选。
