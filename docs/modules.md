# 模块功能说明

## 1. 角色与权限矩阵

| 能力 | 教师 | 学生 |
| --- | --- | --- |
| 创建、编辑、归档课程 | 是 | 否 |
| 通过邀请码加入课程 | 否 | 是 |
| 管理课程成员 | 是 | 否 |
| 上传和删除课程资料 | 是 | 否 |
| 查看已发布资料和知识点 | 是 | 是 |
| 使用课程 AI 问答 | 是 | 是 |
| 生成或发布课程练习 | 是 | 否 |
| 完成练习 | 否 | 是 |
| 创建实验任务和评分规则 | 是 | 否 |
| 提交实验报告 | 否 | 是 |
| 触发、复核和发布批改 | 是 | 否 |
| 查看自己的正式反馈 | 否 | 是 |

平台角色只作为默认权限，所有课程资源还必须校验用户是否属于该课程。

## 2. Auth 模块

职责：注册、登录、刷新会话、获取当前用户、角色守卫。

输入：邮箱、密码、角色。输出：访问令牌、刷新令牌和用户概要。

第一版开放教师和学生注册，暂不验证邮箱归属，仅支持邮箱登录。邮箱仅域名部分不区分大小写，数据库保证邮箱比较值唯一；密码长度为 8–128 个字符，允许空格且不强制字符种类组合，使用 Argon2id 哈希保存。

Access Token 有效期 1 小时，Refresh Token 自登录签发起有效 7 天、不轮换且不续期。注销仅撤销当前 Refresh Token 会话，旧 Access Token 可继续使用至自身过期。前端自行决定 Token 存放位置；请求、刷新和清除规则以 [API 契约](api-contract.md#2-认证接口) 为准。

令牌与会话：

- Access Token 是带到期时间的 HS256 签名令牌，声明包含用户、平台角色、唯一 `jti` 与签发/到期时间；服务端不保存、不维护黑名单，因此注销后旧 Access Token 仍可能有效至自身过期。
- Refresh Token 是密码学安全随机值，数据库只保存其 sha256 摘要；`auth_sessions` 记录用户、签发时间、到期时间和撤销时间。
- 注销只撤销请求指定的那一个会话，其他会话不受影响；不属于当前用户的会话一律不可见。

登录防爆破：失败次数按规范化邮箱（不区分账号是否存在）在滑动窗口内计数，达到阈值后返回 `429`，阈值与窗口配置见 [API 契约 2.6](api-contract.md#26-登录失败限流)。密码错误、账号不存在、账号停用对外返回同一条 `AUTH_INVALID_CREDENTIALS`，且校验耗时相近，避免反推账号是否存在。

本模块对外提供当前用户与平台角色依赖（`CurrentUserDep`、`require_roles`），供其他业务模块在其之上继续做课程级权限判断。

不负责：课程成员权限、课程资源所有权。

## 3. Courses 模块

职责：课程创建、查询、修改、归档、邀请码、加入课程和成员列表；第一版不提供删除或恢复课程接口。请求与响应字段以 [API 契约第 3 节](api-contract.md#3-课程接口) 为准。

课程状态：`ACTIVE`、`ARCHIVED`；新建课程为 `ACTIVE`。

业务规则：

- 只有教师可创建课程；创建教师自动成为课程内 `TEACHER` 成员，只有该教师可以管理课程和查看成员列表。
- 邀请码由服务端生成为 **12 位大写字母与数字**（密码学安全随机），全局唯一，冲突时自动重新生成；客户端按不透明字符串处理，不依赖长度或字符格式。
- 邀请码可重新生成，旧码立即失效；仅创建教师能在未归档课程详情或重置响应中取得邀请码，课程列表和学生响应不含该字段。
- 学生首次使用有效邀请码加入活动课程返回 `201`；重复加入返回 `200` 和课程概要，不新增成员记录，并发请求同样保证唯一。
- 课程列表包含本人参与的活动和归档课程；课程及成员列表均分页。成员列表包含创建教师和已加入学生，不返回邮箱。
- 归档课程只读，不能继续新增资料、任务、提交或发布任务；课程修改、加入及邀请码重置返回 `409 COURSE_ARCHIVED`。已加入学生再次通过邀请码加入归档课程也返回该错误。
- 归档后成员仍可读取课程；重复归档返回当前详情，第一版不支持恢复。
- 课程名称去除首尾空白后为 1–100 字符，说明最多 2000 字符；修改时省略字段保持原值，空字符串说明表示清空，空修改请求和显式 `null` 均不接受。

## 4. Materials 模块

职责：文件上传协议、元数据、解析任务、大纲、章节、知识点与**可检索原文片段**。

支持格式：第一版支持 `.pdf`、`.pptx`、`.docx`，各自的规范 MIME 见 [API 契约 4.2](api-contract.md#42-文件类型与大小)；文件名扩展名与 MIME 必须匹配，不做猜测或纠正。

单文件大小为 1 字节至 50 MiB（52 428 800 字节），上限由 `MATERIAL_MAX_UPLOAD_BYTES` 配置。`sha256` 为 64 位十六进制字符串；应用侧不与对象内容二次比对，内容一致性由对象存储按 `x-amz-checksum-sha256` 在直传时判定。

上传采用预签名直传：初始化（`POST /courses/{id}/materials/uploads`）返回 PUT 地址（10 分钟有效）与确认窗口（24 小时）；完成（`.../{upload_id}/complete`）时服务端确认对象存在且大小、类型、存储侧 SHA-256 与声明一致，才在同一事务中创建资料与 `MATERIAL_PARSE` 任务。重复完成同一上传会话是幂等的，只返回同一资料与同一任务，不产生重复记录。过期未确认的上传会话由独立维护命令锁定，删除其孤立对象并标记 `expired_at`；首次确认始终按 `UPLOAD_INVALID` 拒绝。

权限与检查顺序：只有课程创建教师能初始化与完成；学生 `403 ROLE_FORBIDDEN`，其他教师 `403 COURSE_FORBIDDEN`，归档课程 `409 COURSE_ARCHIVED`，三者都先于元数据校验发生，因此失败请求不会留下上传会话。资料详情、解析任务状态查询、资料列表与大纲查询已开放给课程成员；删除与重试解析仅课程创建教师可用（契约 5.2/5.3）。

解析状态：`UPLOADING`、`UPLOADED`、`PROCESSING`、`READY`、`FAILED`；任务状态：`PENDING`、`RUNNING`、`SUCCEEDED`、`FAILED`、`CANCELLED`。

解析 Worker（契约 5.5）：完成上传与重试解析后由后台任务执行解析，推进任务 `PENDING → RUNNING → SUCCEEDED/FAILED` 与资料 `PROCESSING → READY/FAILED`；解析结果（章节、知识点与**可检索原文片段**）在同一事务内一次性落库，失败只写安全摘要、不产生部分数据。片段是课程问答的检索单位：按约 1,000 字符切分、相邻片段重叠约 100 字符，并记录来源位置（PDF 页码 / PPTX 幻灯片号 / DOCX 段落序号）；片段先清后写，因此重复解析不会累积，删除资料时一并清空。第一版不做扫描版 PDF 的 OCR；通用重试接口 `POST /jobs/{job_id}/retry` 已在第 10 节交付，
但**资料解析的重试仍应经由 `POST /materials/{material_id}/parse`**（契约 5.3），
通用接口对 `MATERIAL_PARSE` 返回 `409 JOB_NOT_RETRYABLE`。

## 5. Learning 模块

职责：课程问答、引用展示、练习生成、答题和结果反馈。

业务规则（问答部分以 [API 契约第 6 节](api-contract.md#6-课程问答接口) 为准）：

- 问答只检索当前课程中**未删除**、状态为 `READY` 的资料，检索单位是资料的**原文片段**（见第 4 节）。
- 每条有事实依据的回答至少返回一个引用，引用可核对到原文（资料、章节、来源类型与位置）；
  无依据时明确提示"课程资料中未找到依据"，不编造来源，也不算错误。
- 会话属于创建者本人：教师与学生各自只能看到自己的会话；消息接口仅会话所有者可用。
- 归档课程可以读取历史会话与消息，但不能新增会话或提问。
- 练习题保存题干、类型、选项、标准答案、解析和知识点。
- 第一版支持单选题、判断题和简答题。

实现说明（后端，第一版只交付后端问答能力）：

- **检索**：首版使用 PostgreSQL `pg_trgm` 文本检索（未配置嵌入模型或 pgvector）；查询本身限定课程 ID、资料未删除且 `READY`，最多取 5 个片段；部署环境须提供 `pg_trgm` 扩展。片段来自解析 Worker（第 4 节），早于该功能解析完成的资料由 `scripts/backfill_material_chunks.py` 回填——**开放问答前必须执行**。
- **生成**：通过可替换的 AI 适配层调用 Chat Completions 兼容端点（复用 `AI_BASE_URL` / `AI_MODEL` / `AI_API_KEY`）；模型输出经 Pydantic 校验，只允许引用本次检索到的片段，服务端再核对片段 ID 与原文摘录后才生成引用，全部核对失败时降级为"无依据"。模型调用期间不持有数据库事务，成功后一问一答在同一事务写入；同一会话并发提问返回 `409 CHAT_CONFLICT` 且不写入半组消息。
- **可回溯**：每次生成都留下尝试记录（模型名称、提示词版本、片段数量、耗时与安全失败摘要），日志与记录都不包含完整课件原文、提示词或密钥。
- **第一版不含**：聊天页面与引用跳转页面（前端）。

练习模块（第 7 节）实现说明：

- **题目由独立 Worker 生成**：`POST /courses/{id}/practice-sets/generate` 只创建练习记录（`GENERATING`）与 `PRACTICE_GENERATE` 任务，`scripts/practice_worker.py` 以 `FOR UPDATE SKIP LOCKED` 领取、带租约与运行令牌执行；**模型调用期间不持有数据库事务**，读取片段上下文后即结束只读事务再调用模型。
- **服务端兜住配额与来源**：题型配额按请求顺序均衡分配（余数给靠前题型）；模型输出的题数、题型配额、单选选项（2–6 个且规范化后不重复）、判断题布尔答案、简答题评分要点（2–6 个）与每题来源片段/摘录都由服务端校验，选项 ID 由服务端生成；摘录无法在片段原文中找到即整次失败，**不落库部分题目**。
- **回写顺序固定**：课程 → 练习 → 任务 → 按 ID 排序的来源资料；复查课程仍活动、练习仍在生成、运行令牌匹配、来源资料仍可用后一次性写入题目并置 `DRAFT`。课程在生成期间归档 → `CANCELLED`；资料失效、模型失败或输出非法 → `FAILED`。旧执行者在重试后因令牌不匹配无法回写。
- **题目不可变**：发布后不可修改；题目携带来源快照（资料 ID、名称、片段定位与原文摘录），资料被软删除或重新解析不影响已发布练习。
- **评分**：所有题等权、百分制两位小数；单选与判断完全匹配得满分；简答题对文本做 Unicode NFKC、大小写、空白与常见标点规范化后按要点计分，每个要点只计一次，命中全部要点才算完全正确。`(practice_set_id, student_id)` 唯一约束保证每生每题集只提交一次。
- **答案可见性**：课程教师（含创建者）可见标准答案、评分要点与解析；学生视角下这些字段为 `null`/空数组，只能在自己提交后的答题结果中看到。
- **前端尚未接入**：练习生成、答题与结果页面，以及第 8 节及之后的模块。

## 6. Assignments 模块

职责：实验任务、截止时间、附件、评分规则、发布和关闭。

任务状态：`DRAFT`、`PUBLISHED`、`CLOSED`、`ARCHIVED`。

业务规则：

- 评分项总分必须等于任务总分。
- 草稿仅教师可见。
- 发布后允许修改说明，但评分规则发生变化时必须记录版本。
- 截止后是否允许补交由任务字段控制。

实现说明（第 8 节六个接口，迁移 `0010_assignments`）：

- **状态机**：`创建 → DRAFT`、`DRAFT → PUBLISHED`、`PUBLISHED → CLOSED`；重复发布与重复关闭**幂等**（不覆盖 `published_at` / `closed_at`），`DRAFT` 不能关闭，`CLOSED`/`ARCHIVED` 不能修改或重新发布。`ARCHIVED` 本轮只作为兼容状态，没有单独归档接口；截止时间不会由查询接口自动把状态改成 `CLOSED`。
- **三张表**：`assignments`（任务本体 + `current_rubric_version_id` 指针）、`assignment_rubric_versions`（评分版本，`(assignment_id, version)` 唯一、`version >= 1`、`total_score > 0`）、`assignment_rubric_items`（版本下的评分项，`(rubric_version_id, order)` 唯一、`max_score > 0`、`order > 0`）。`assignments` 与版本表互相引用（循环外键），因此创建时按"任务（指针 NULL）→ 版本 → 评分项 → 回填指针"逐步落库；删除任务时级联删除版本与评分项。
- **评分规则版本策略**：创建时建立版本 1。版本一经写入**不原地修改**；只有 `total_score` 或评分项发生实质变化（标题/说明/分值/顺序的任一项不同）时才追加 `current_version + 1`，并生成**新的评分项 ID**；非评分字段修改与"提交完全相同规则"都不产生新版本。详情只返回当前版本，历史版本保留在库中供后续批改使用（暂无公开查询接口）。
- **总分校验**：请求字段结构校验通过后才比较"评分项之和 == 总分"，使用 `Decimal` 精确比较（不使用二进制浮点），不匹配返回 `422 RUBRIC_SCORE_MISMATCH`；修改时用"数据库当前值 + 本次提供字段"组成候选结果再校验。
- **权限与可见性**：创建/修改/发布/关闭仅课程创建教师（学生 `403 ROLE_FORBIDDEN`，其他教师 `403 COURSE_FORBIDDEN`，非成员 `404`）；学生列表与详情只看到 `PUBLISHED`/`CLOSED`/`ARCHIVED`，**草稿在 SQL 查询层排除**；归档课程可读历史但写操作 `409 COURSE_ARCHIVED`。
- **错误优先级**：认证 → 资源可见性 → 角色 → 归档/状态 → 请求体结构与字段 → `RUBRIC_SCORE_MISMATCH` → 写入。实现方式与练习模块一致：路由只读原始 `Request`，守卫依赖先加锁并完成检查，再由服务手工解析请求体（OpenAPI 用显式 `requestBody` 声明）。
- **统一锁顺序**：课程行 → 任务行 → 当前评分版本；读接口不加写锁。
- **Submission 接入点**（第 9 节消费）：`service.can_submit(assignment, now)` 判断"是否允许提交"（`PUBLISHED` 且未截止或允许补交；`now == due_at` 视为已截止；手工关闭优先于 `allow_late_submission`），`service.current_rubric_version_id(assignment)` 给出批改应使用的评分版本。**报告上传、提交、AI 批改、教师复核与成绩发布属第 9 节，本轮未交付**，任务附件也没有公开接口定义。
- **前端尚未接入**：任务创建/修改/发布/关闭页面；前端 mock 与正式契约的差异（分页包装、列表使用摘要 Schema）见 `docs/api-contract.md` 8.13。

## 7. Grading 模块

职责：学生提交、报告解析、AI 分项批改、教师复核、结果发布。

提交状态：`UPLOADING`、`SUBMITTED`、`GRADING`、`REVIEW_REQUIRED`、`PUBLISHED`、`FAILED`。

批改项至少包含：

- 评分项 ID 和名称
- AI 建议得分
- 教师最终得分
- 判断说明
- 报告原文证据或定位
- 错误类型
- 修改建议

正式成绩必须由教师发布。学生只能查看自己的已发布结果。

实现说明（第 9 节八个接口，迁移 `0012_submissions_grading`）：

- **一张提交一份**：`submissions` 上 `(assignment_id, student_id)` 唯一，并发由数据库兜住；
  初始化上传时复用仍处于 `UPLOADING` 的提交（同一 `submission_id`）并签发**新的**上传会话与
  对象键，被替代的旧会话由维护命令清理；报告正式提交后再次初始化返回 `409 SUBMISSION_ALREADY_EXISTS`。
- **五张表**：`submissions`（提交本体 + 固定评分版本指针）、`submission_upload_sessions`
  （每次上传尝试与完成快照）、`grade_reviews`（每份提交唯一一条，AI 原始值与教师终稿并存）、
  `grade_items`（评分项快照 + AI/教师分数，`rubric_item_id` 为**历史外键**）、
  `submission_grade_attempts`（每次 Worker 尝试的审计轨迹）。原生枚举 `submission_status`、
  `submission_grade_attempt_status`。
- **固定评分版本**：完成提交时写入当时的 `current_rubric_version_id`；批改、复核与展示都只读
  提交引用的版本，教师之后修改 Rubric（新版本）不影响历史提交。
- **上传协议复用课件上传**：对象键由课程、任务与上传会话 UUID 推导（不含用户文件名），只接受
  PDF / DOCX（拒绝旧版 `.doc`），完成时用 HeadObject 校验大小、MIME 与存储侧 SHA-256；
  独立配置 `SUBMISSION_MAX_UPLOAD_BYTES`（默认 50 MiB）与 `SUBMISSION_UPLOAD_CONFIRM_TTL_SECONDS`
  （默认 24 小时），并提供预签名 GET（`download_url` / `download_expires_at`）。
- **完成与会话不变量**：每份提交最多只有一个**完成**的上传会话（部分唯一索引
  `(submission_id) WHERE completed_at IS NOT NULL`）。完成请求的状态检查全部在 HeadObject
  之前，优先级为 已完成幂等 → 已被其他会话提交（409）→ 已过期/被清理（`UPLOAD_EXPIRED`）→
  已被替代或对象键不匹配（`UPLOAD_SUPERSEDED`）→ 提交状态 → 允许提交 → 对象确认；
  只有命中上述命名唯一约束的 `IntegrityError` 才转成 `409`，其他数据库错误原样抛出。
- **统一锁顺序**：课程 → Assignment → 提交固定 RubricVersion → Submission →（UploadSession /
  Job / GradeReview）。初始化、完成、触发、重试、复核与发布都按此顺序取锁；读接口不加写锁。
- **批改 Worker**（`scripts/grading_worker.py`）沿用练习 Worker 的领取、租约、运行令牌与心跳协议；
  模型调用与对象下载期间**不持有数据库事务**，成功回写一次性创建 Review 与全部 GradeItem，
  失败与旧执行者都不写部分结果；生成期间课程归档则任务 `CANCELLED`、提交 `FAILED`。
  提取阶段保留 PDF 页码 / DOCX 段落号，模型必须声明证据位置区间，服务端核对
  "位置存在 + 摘录落在区间内"，来源类型由报告 MIME 确定。
- **复核与发布**：AI 建议自动复制为初始终稿但 `reviewed_at` 仍为空；`PATCH` 必须提交**完整快照**
  （恰好覆盖提交引用版本的全部评分项），保存后写 `reviewed_by` / `reviewed_at` 并保留 AI 原始字段，
  因此 AI 建议与教师终稿可同时审计；发布要求已复核，重复发布幂等且不覆盖首次 `published_at`。
- **学生可见性**：发布前批改详情对学生一律 `404`；发布后返回终稿摘要、最终分、教师评语与证据及定位
  （`evidence_quote` / `evidence_source_type` / `evidence_location_start` / `evidence_location_end`），
  AI 原始建议分（`ai_score` / `ai_comment` / `suggested_total_score` / `ai_summary`）为 `null`。
- **错误优先级**：认证 → 资源可见性 → 角色 → 课程归档 → 业务状态 → 请求结构 → 字段与分数语义 → 写入；
  分数只接受 JSON number、最多两位小数且不超过该项满分，教师最终总分由分项求和（`Decimal`）。
- **本轮未交付**：真实模型效果验收、前端提交/批改页面接入、部署环境 Worker 常驻运行验收；
  报告附件的批量管理与删除流水线。

## 8. Jobs 模块

职责：统一管理资料解析、练习生成和报告批改的异步状态。

**公开任务范围**（契约 10.0）：`MATERIAL_PARSE / MATERIAL`、`PRACTICE_GENERATE / PRACTICE_SET`、
`SUBMISSION_GRADE / SUBMISSION` 三类。Agent 的 `AGENT_RUN / AGENT_RUN` 是**内部任务**，
只通过 `/agent-runs` 系列接口读写；两个通用 Jobs 路由对它统一返回 `404 RESOURCE_NOT_FOUND`。

任务状态：`PENDING`、`RUNNING`、`SUCCEEDED`、`FAILED`、`CANCELLED`。

响应（契约 10.0）：`id`、`type`、`status`、`progress`（**0–100**）、`resource_type`、
`resource_id`、`error`（最长 **500** 字符的安全摘要）、`created_at`、`started_at`、`finished_at`
（三个时间字段导出 `format: date-time`）。响应**不暴露** `attempts`、`run_token`、
`lease_expires_at` 等内部调度字段。

### 实现说明（第 10 节，迁移 `0013_jobs_contract`）

- **公开枚举与内部枚举分离**：ORM 的 `JobType` / `JobResourceType` 保留 `AGENT_RUN`
  供 Agent Worker 使用；响应 Schema 定义只含三类公开任务的同名枚举（组件名保持
  `JobType` / `JobResourceType`），内部枚举在 Schema 模块以 `DbJobType` 别名引用，
  转换在字段校验器中按取值完成（`AGENT_RUN` 会校验失败，属防御性兜底）。
- **数据库约束**（`0013_jobs_contract`）：`ck_jobs_progress_range`（`progress BETWEEN 0 AND 100`）
  与 `ck_jobs_type_resource_match`（类型与资源类型必须配对，含内部 `AGENT_RUN / AGENT_RUN`）；
  受项目命名约定影响，落库名为 `ck_jobs_ck_jobs_progress_range` 与
  `ck_jobs_ck_jobs_type_resource_match`，表达式与 ORM `Job.__table_args__` 逐字一致。
- **显式分派**：查询与重试都先校验"类型/资源类型配对"，公开范围之外直接 404，
  不再依赖"查错资源后偶然得到 404"；三类公开任务分别调用 Materials / Practice / Grading
  各自的资源权限服务。重试目标是强类型的两种结果（练习 / 提交批改），
  移除 `object | None` 式弱类型传递。
- **检查顺序与锁**（契约 10.2）：认证 → 公开范围 → 关联资源可见性 → 角色 →
  课程归档 → 状态 → 请求体 → 写入；状态判定在按 **课程 → 资源 → 任务** 取得行锁**之后**完成。
- **职责边界**：Jobs 只做查找、权限优先级与类型分派；状态重置由资源模块负责，
  Worker 的领取、心跳与回写协议不变（解析 5.5、练习 7.10、批改 9.11）。

任务需要支持幂等触发、失败原因、进度、开始时间、结束时间和安全重试。

## 9. Dashboard 模块

职责：聚合教师和学生首页所需数据，不拥有核心业务数据。

教师首页：课程数、待批改数、最近提交和资料处理失败提示。

学生首页：进行中课程、待完成任务、最近反馈和资料处理状态。

第一版只做基础统计，不实现复杂学情预测。

## 10. AI 基础层

职责：模型适配、提示词版本、文档解析、检索、引用和结构化输出。

要求：

- 业务模块不直接调用具体模型 SDK。
- 所有 AI 输出使用 Pydantic Schema 校验。
- 保存模型名称、提示词版本、耗时和失败信息。
- 日志中不得记录密码、令牌或完整敏感文档。
- 可以替换模型供应商而不改变业务 API。
