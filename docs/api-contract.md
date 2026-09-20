# API 开发契约

## 1. 通用约定

- 基础路径：`/api/v1`
- 数据格式：`application/json; charset=utf-8`
- 认证：`Authorization: Bearer <access_token>`
- 标识符：UUID 字符串
- 时间：ISO 8601 UTC，例如 `2026-09-18T08:30:00Z`
- 列表分页：`page` 从 1 开始，`page_size` 默认 20，最大 100
- 创建成功：`201 Created`
- 异步任务已受理：`202 Accepted`
- 删除成功：`204 No Content`
- 请求追踪：服务端透传或生成 `X-Request-ID`，并在错误响应的 `request_id` 与响应头中返回同一值
- 健康检查：`GET /health/live` 与 `GET /health/ready` 不在 `/api/v1` 前缀下，供平台探针直接访问

分页响应：

```json
{
  "items": [],
  "page": 1,
  "page_size": 20,
  "total": 0
}
```

错误响应：

```json
{
  "error": {
    "code": "COURSE_FORBIDDEN",
    "message": "你没有访问该课程的权限",
    "details": {},
    "request_id": "uuid"
  }
}
```

## 2. 认证接口

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/auth/register` | 注册教师或学生账户 | 公开 |
| POST | `/auth/login` | 登录 | 公开 |
| POST | `/auth/refresh` | 刷新访问令牌 | 刷新令牌 |
| POST | `/auth/logout` | 注销当前会话 | 已登录 |
| GET | `/users/me` | 当前用户资料 | 已登录 |

### 2.1 注册与登录

比赛初期开放注册，任何人均可选择 `TEACHER` 或 `STUDENT` 角色，无需教师邀请码或管理员审批。第一版暂不验证邮箱归属，注册成功后即可使用邮箱和密码登录；邮箱仅作为登录标识，不代表已验证持有人身份。

邮箱规则：

- 仅支持邮箱登录，不支持用户名登录。
- 保存原始邮箱用于展示；用于注册查重和登录查询的比较值仅将域名部分规范化为小写，本地部分（`@` 前）保留大小写。
- `Teacher@EXAMPLE.com` 与 `Teacher@example.com` 视为同一账号；`Teacher@example.com` 与 `teacher@example.com` 视为不同账号。
- 不移除本地部分中的句点或 `+` 后缀。数据库对邮箱比较值设置唯一约束，以处理并发注册；注册和登录使用同一比较规则。

密码规则：

- 长度为 8–128 个字符，允许空格，不强制大小写字母、数字或特殊字符的组合。
- 密码区分大小写，不自动去除首尾空格，也不截断超长输入。
- 密码仅保存 Argon2id 哈希，响应和日志不得包含密码或密码哈希。

注册请求：

```json
{
  "email": "teacher@example.com",
  "password": "Demo password 2026!",
  "display_name": "张老师",
  "role": "TEACHER"
}
```

注册响应为 `201 Created`，只返回账号资料，不返回令牌：

```json
{
  "id": "uuid",
  "email": "teacher@example.com",
  "display_name": "张老师",
  "role": "TEACHER",
  "created_at": "2026-09-18T08:30:00Z"
}
```

`email` 原样保存用户提交的大小写，仅域名部分参与查重比较。注册邮箱已被占用（包括并发注册被数据库唯一约束拦下的情况）返回 `409` 与 `AUTH_EMAIL_TAKEN`；成功后由前端用同一凭证调用登录接口。

登录请求使用 JSON：

```json
{
  "email": "teacher@example.com",
  "password": "Demo password 2026!"
}
```

登录响应：

```json
{
  "access_token": "string",
  "refresh_token": "string",
  "token_type": "bearer",
  "expires_in": 3600,
  "user": {
    "id": "uuid",
    "display_name": "张老师",
    "role": "TEACHER"
  }
}
```

### 2.2 Token 生命周期与传输

| 项目 | 第一版规则 |
| --- | --- |
| Access Token 有效期 | 3600 秒（1 小时），响应中的 `expires_in` 以秒为单位 |
| Refresh Token 有效期 | 自登录签发起 7 天 |
| 前端存储 | 由前端自行选择存放位置和内部键名，API 契约不作限制 |
| Token 传输 | 登录通过 JSON 响应返回两个 Token；普通受保护接口使用 `Authorization: Bearer <access_token>`；刷新接口通过 JSON 请求体提交 Refresh Token |
| Refresh Token 轮换 | 第一版不轮换；同一个 Refresh Token 可在有效期内重复使用，刷新不延长其有效期 |
| 注销后的 Refresh Token | 服务端撤销对应会话，立即停止允许该 Token 刷新 |
| 注销后的 Access Token | 不维护黑名单；已签发的 Access Token 可能继续有效至自身过期 |

登录成功后，前端自行管理两个 Token。刷新成功后只更新 Access Token，保留原 Refresh Token。刷新失败或注销后，前端清除其保存的两个 Token。响应字段名不规定前端的内部存储键名。

前端存放位置的选择须满足上述请求协议；若采用由服务端设置的 HttpOnly Cookie，需要另行修改传输契约。

### 2.3 服务端会话记录

服务端在数据库中保存 Refresh Token 对应的会话记录，使用 `auth_sessions` 表保存以下字段：

| 字段 | 含义 |
| --- | --- |
| `id` | 会话 UUID |
| `user_id` | 所属用户 UUID |
| `refresh_token_hash` | Refresh Token 的哈希值，不保存 Token 明文 |
| `expires_at` | 登录签发时间加 7 天，刷新时不更新 |
| `revoked_at` | 撤销时间；未撤销时为 `NULL` |
| `created_at` | 会话创建时间 |

登录成功时生成 Refresh Token，保存对应哈希和会话记录，并将 Token 明文放入登录响应返回前端。会话时间字段遵循通用约定中的 ISO 8601 UTC 格式。第一版不引入 Token Family、轮换计数或重放检测机制。

### 2.4 刷新访问令牌

请求使用 JSON Body 提交 Refresh Token，不要求有效的 Access Token：

```http
POST /api/v1/auth/refresh
Content-Type: application/json
```

```json
{
  "refresh_token": "string"
}
```

服务端使用提交的 Refresh Token 的哈希查找会话，仅当会话存在、`expires_at > now` 且 `revoked_at IS NULL` 时允许刷新。

刷新成功响应：

```json
{
  "access_token": "new-access-token",
  "token_type": "bearer",
  "expires_in": 3600
}
```

响应不包含新的 `refresh_token`，原 Refresh Token 及其到期时间保持不变。

会话不存在、已过期或已撤销时，返回 `401 Unauthorized`，响应体沿用通用约定中的 `error` 对象结构（`code`、`message`、`details`、`request_id`）。刷新失败后，前端清除其保存的两个 Token，并跳转 `/login`。

### 2.5 注销当前会话

注销需要有效的 Access Token，并通过 JSON Body 指定当前会话的 Refresh Token：

```http
POST /api/v1/auth/logout
Authorization: Bearer <access_token>
Content-Type: application/json
```

```json
{
  "refresh_token": "string"
}
```

服务端验证 Bearer 凭据，使用 Refresh Token 的哈希查找会话，并确认会话的 `user_id` 与当前认证用户一致后，将该会话的 `revoked_at` 设置为当前时间。不得撤销属于其他用户的会话。注销只撤销请求指定的当前会话，不影响该用户的其他会话。

如果 Access Token 已过期，客户端先调用刷新接口，再使用新 Access Token 调用注销接口；若刷新失败，则按刷新失败流程清除两个 Token 并跳转 `/login`。

注销成功后，前端清除其保存的两个 Token。服务端已撤销的 Refresh Token 再次用于刷新时返回 401。仅删除前端 Token 不代表服务端会话已经撤销。

第一版不维护 Access Token 黑名单，因此已经被单独保存的旧 Access Token 仍可能使用到其自身的 1 小时有效期结束；注销不会立即使其失效。

注销成功返回 `204 No Content`，无响应体。请求体中的 Refresh Token 不存在或不属于当前认证用户时返回 `404` 与 `RESOURCE_NOT_FOUND`，两种情况使用同一响应，避免用于枚举他人会话。重复注销同一会话视为成功。

### 2.6 登录失败限流

按规范化邮箱（契约 2.1 的比较值）统计登录失败次数，防止针对单一账号的密码爆破：

| 项目 | 第一版规则 |
| --- | --- |
| 统计窗口 | 最近 15 分钟（滑动窗口） |
| 失败上限 | 5 次 |
| 计数对象 | 不区分账号是否存在；密码错误与账号不存在都计入 |
| 触发结果 | 窗口内失败次数达到上限后，后续登录请求返回 `429` 与 `AUTH_TOO_MANY_ATTEMPTS`，`details.retry_after_seconds` 为最早一次失败滚出窗口所需的秒数 |
| 清除时机 | 该邮箱登录成功时清空其全部失败记录 |
| 是否延长窗口 | 不延长；被拒绝的请求本身不计入失败次数 |

限流以规范化邮箱为唯一维度，不使用 IP 或设备指纹；被限流的响应与账号是否存在无关。

### 2.7 当前用户资料

`GET /users/me` 需要有效的 Access Token，返回：

```json
{
  "id": "uuid",
  "email": "teacher@example.com",
  "display_name": "张老师",
  "role": "TEACHER",
  "created_at": "2026-09-18T08:30:00Z"
}
```

Access Token 缺少、格式错误、签名不符或已过期时返回 `401` 与 `AUTH_TOKEN_EXPIRED`，前端据此清除本地令牌并跳转 `/login`。

## 3. 课程接口

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/courses` | 创建课程 | 教师 |
| GET | `/courses` | 获取我参加的课程 | 已登录 |
| GET | `/courses/{course_id}` | 课程详情 | 课程成员 |
| PATCH | `/courses/{course_id}` | 修改课程 | 课程教师 |
| POST | `/courses/{course_id}/archive` | 归档课程 | 课程教师 |
| POST | `/courses/{course_id}/invite-code` | 重新生成邀请码 | 课程教师 |
| POST | `/courses/join` | 使用邀请码加入 | 学生 |
| GET | `/courses/{course_id}/members` | 课程成员列表 | 课程教师 |

### 3.1 通用规则与权限

本节定义课程模块的请求与响应 Schema，供后续实现和前端联调使用。所有接口使用 Bearer 认证，路径均相对于 `/api/v1`；UUID、UTC 时间、分页和统一错误响应遵循第 1 节。

- 课程状态 `CourseStatus` 为 `ACTIVE` 或 `ARCHIVED`；新建课程为 `ACTIVE`。
- 教师创建课程时，服务端设置 `teacher_id` 为当前用户，并自动创建其 `TEACHER` 课程成员记录。
- 第一版由创建教师管理课程；平台角色为教师不代表可以管理其他教师的课程。
- 课程成员可读取课程详情；只有创建教师可修改、归档、重置邀请码和读取成员列表。
- 课程列表包含本人参与的活动课程及归档课程；课程和成员列表均使用 `page`、`page_size`，不返回完整无分页列表。
- 归档课程保持可读，不提供恢复接口。修改、加入或重置邀请码返回 `409 COURSE_ARCHIVED`；重复归档返回 `200` 和当前详情，不重复改变归档结果。
- 邀请码由服务端生成，为 12 位大写字母与数字；客户端按不透明字符串提交与展示，不依赖其长度或字符格式。
- 邀请码仅在创建教师查看未归档课程的详情，以及重置邀请码的响应中返回。课程列表、学生响应和已归档课程详情均省略 `invite_code` 字段，不返回 `null`。
- 邀请码全局唯一；重置后旧码立即失效，旧码加入返回 `422 INVITE_CODE_INVALID`。

### 3.2 请求 Schema

课程 JSON 请求拒绝未声明字段和显式 `null`，校验失败返回 `422 VALIDATION_ERROR`。课程 ID、教师 ID、状态和时间由服务端维护，不能通过创建或修改请求指定。

| Schema | 字段 | 类型 | 必填 | 规则 |
| --- | --- | --- | --- | --- |
| `CourseCreateRequest` | `name` | string | 是 | 去除首尾空白后为 1–100 个字符，不能全为空白 |
| `CourseCreateRequest` | `description` | string | 否 | 最多 2000 个字符；省略时默认为空字符串 |
| `CourseUpdateRequest` | `name` | string | 否 | 提供时按创建规则校验；省略时保持原值 |
| `CourseUpdateRequest` | `description` | string | 否 | 最多 2000 个字符；省略时保持原值，传空字符串表示清空 |
| `CourseJoinRequest` | `invite_code` | string | 是 | 非空字符串；客户端按不透明字符串提交，不依赖示例中的长度或字符格式 |

`CourseUpdateRequest` 至少包含 `name`、`description` 中的一项，空对象返回 `422 VALIDATION_ERROR`。归档和重置邀请码接口无请求体。

创建课程请求：

```json
{
  "name": "软件工程实验",
  "description": "课程说明"
}
```

修改课程请求示例（清空说明）：

```json
{ "description": "" }
```

加入课程请求：

```json
{ "invite_code": "AB12CD34EF56" }
```

### 3.3 响应 Schema

`CourseSummary` 的以下字段始终存在且不为 `null`：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `id` | UUID string | 课程 ID |
| `name` | string | 课程名称 |
| `description` | string | 课程说明；未填写时为空字符串 |
| `teacher_id` | UUID string | 创建教师的用户 ID |
| `status` | `ACTIVE` / `ARCHIVED` | 课程状态 |
| `created_at` | ISO 8601 UTC string | 创建时间 |
| `updated_at` | ISO 8601 UTC string | 最近更新时间 |

`CourseDetail` 包含 `CourseSummary` 全部字段，不含 `invite_code`。`CourseDetailWithInviteCode` 在此基础上要求非空字符串 `invite_code`。创建和修改课程返回含邀请码详情；归档返回普通详情。读取详情时，创建教师查看未归档课程返回 `CourseDetailWithInviteCode`，其他成员或归档课程返回 `CourseDetail`。OpenAPI 分别声明这两种结构，不把邀请码声明为可返回 `null`。

教师创建课程的 `201` 响应示例：

```json
{
  "id": "70d1bdfa-bb1a-4b22-9f13-9f1398aeb53c",
  "name": "软件工程实验",
  "description": "课程说明",
  "teacher_id": "a5e675f0-696c-4970-a453-e05c85d4a9e9",
  "status": "ACTIVE",
  "created_at": "2026-09-19T08:30:00Z",
  "updated_at": "2026-09-19T08:30:00Z",
  "invite_code": "AB12CD34EF56"
}
```

服务端生成 **12 位大写字母与数字** 的邀请码；示例仅用于说明，
客户端必须按不透明字符串处理，不依赖其长度或字符格式。

`CourseMemberSummary` 的以下字段始终存在且不为 `null`；不返回成员邮箱：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `user_id` | UUID string | 成员用户 ID |
| `display_name` | string | 成员显示名称 |
| `course_role` | `TEACHER` / `STUDENT` | 课程内角色 |
| `joined_at` | ISO 8601 UTC string | 加入时间；创建教师为课程创建时的成员记录时间 |

成员列表包含创建教师和已加入学生。`Page<T>` 表示第 1 节的分页对象，其中 `items` 为对应类型的数组，`page`、`page_size`、`total` 为整数。

### 3.4 接口成功响应与幂等行为

| 方法与路径 | 请求 | HTTP | 响应 Schema |
| --- | --- | --- | --- |
| `POST /courses` | `CourseCreateRequest` | 201 | `CourseDetailWithInviteCode` |
| `GET /courses` | 分页查询参数 | 200 | `Page<CourseSummary>` |
| `GET /courses/{course_id}` | 无请求体 | 200 | `CourseDetailWithInviteCode` 或 `CourseDetail`，按邀请码可见性决定 |
| `PATCH /courses/{course_id}` | `CourseUpdateRequest` | 200 | `CourseDetailWithInviteCode` |
| `POST /courses/{course_id}/archive` | 无请求体 | 200 | `CourseDetail`，状态为 `ARCHIVED`，不含邀请码 |
| `POST /courses/{course_id}/invite-code` | 无请求体 | 200 | `{ "invite_code": "string" }` |
| `POST /courses/join` | `CourseJoinRequest` | 201 / 200 | `CourseSummary` |
| `GET /courses/{course_id}/members` | 分页查询参数 | 200 | `Page<CourseMemberSummary>` |

学生使用当前有效邀请码首次加入时返回 `201`；已是该课程成员时返回 `200` 和现有课程信息，不创建重复成员记录。并发重复加入也必须保证成员记录唯一。幂等加入仅适用于活动课程及当前有效邀请码：旧码失效后返回 `422 INVITE_CODE_INVALID`；归档课程返回 `409 COURSE_ARCHIVED`，即使学生已经加入。

重置邀请码成功后旧码立即失效，响应中的新码用于后续加入。客户端从课程详情重新取得当前邀请码，不应依赖本地缓存的旧码。

### 3.5 课程错误响应

所有错误沿用第 1 节的 `error` 对象和请求追踪规则。

| 场景 | HTTP | 错误码 |
| --- | --- | --- |
| 缺少、无效或过期的 Access Token | 401 | `AUTH_TOKEN_EXPIRED` |
| 学生调用教师接口，或教师调用学生加入接口 | 403 | `ROLE_FORBIDDEN` |
| 非课程成员读取详情，或非创建教师管理课程、读取成员列表 | 403 | `COURSE_FORBIDDEN` |
| 指定的课程不存在 | 404 | `RESOURCE_NOT_FOUND` |
| 对归档课程执行修改、加入或重置邀请码 | 409 | `COURSE_ARCHIVED` |
| 邀请码不存在或已被重置失效 | 422 | `INVITE_CODE_INVALID` |
| 请求字段、UUID、分页参数不合法，或修改请求为空 | 422 | `VALIDATION_ERROR` |

通过课程 ID 操作时，完成认证和资源权限检查后才返回归档状态错误，避免向无权访问者暴露课程状态。

## 4. 课件上传协议

课件文件不写入应用实例本地磁盘，统一由浏览器使用预签名地址直传对象存储。本节冻结第一版落地的 **4 个接口**：两个写入接口（初始化、完成）与两个状态查询接口（资料详情、任务状态）。第 5 节表中其余资料接口属于后续阶段，以本节定义的资料与任务结构为准。

| 方法 | 路径 | 说明 | 权限 | 本节 |
| --- | --- | --- | --- | --- |
| POST | `/courses/{course_id}/materials/uploads` | 初始化上传 | 课程教师 | 4.3 |
| PUT | 预签名 `upload_url` | 浏览器直传对象存储（不经后端） | 地址自带签名 | 4.4 |
| POST | `/courses/{course_id}/materials/uploads/{upload_id}/complete` | 完成上传并创建解析任务 | 课程教师 | 4.5 |
| GET | `/materials/{material_id}` | 资料详情与处理状态 | 课程成员 | 4.7 |
| GET | `/jobs/{job_id}` | 任务状态 | 任务关联资料的课程成员 | 4.7 |

### 4.1 上传流程

1. 教师调用初始化接口，提交文件名、MIME、字节大小和 sha256。
2. 服务端校验通过后创建上传会话（`upload_id`），返回 `201` 与预签名 `upload_url`、`method`、`headers`、`expires_at`、`confirm_deadline_at`。
3. 浏览器在 `expires_at` 之前，使用 `method` 与 `headers` 把文件字节流 PUT 到 `upload_url`。
4. 教师调用完成接口；服务端确认对象存在且大小与声明一致，创建资料记录与 `MATERIAL_PARSE` 任务，返回 `202`。
5. 前端用 `GET /materials/{material_id}` 与 `GET /jobs/{job_id}` 轮询状态。

完成确认后资料状态为 `PROCESSING`、任务状态为 `PENDING`，由解析 Worker 推进（见 5.5）；状态查询接口返回的 `PROCESSING` + `PENDING` 表示排队或处理中，不是失败。

### 4.2 文件类型与大小

| 扩展名（比较时忽略大小写） | 规范 MIME |
| --- | --- |
| `.pdf` | `application/pdf` |
| `.pptx` | `application/vnd.openxmlformats-officedocument.presentationml.presentation` |
| `.docx` | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` |

- `filename` 去除首尾空白后为 1–255 个字符，不能只包含空白；不得包含 `/`、`\` 或空字节。服务端保存去除首尾空白后的文件名，并原样保留大小写。
- `filename` 必须以上表扩展名之一结尾；扩展名比较不区分大小写。
- `content_type` 必须**精确等于**该扩展名对应的规范 MIME。不接受 `application/octet-stream`、近似类型或带参数的形式（例如 `application/pdf; charset=utf-8`）。
- 扩展名与 MIME 不匹配时以 `UPLOAD_INVALID` 拒绝，服务端不做猜测、纠正或规范化。
- `size` 为对象字节数，取值 1 – 上限。上限默认 **50 MiB（52 428 800 字节）**，由部署配置 `MATERIAL_MAX_UPLOAD_BYTES` 决定；服务端按启动时生效值校验，并在超限错误的 `details.max_size_bytes` 中回显当前上限。
- `sha256` 为 64 位十六进制字符串（`^[0-9a-fA-F]{64}$`），大小写均可接受，服务端统一按小写持久化。服务端**不在应用侧**重新读取文件做比对，而是把该摘要以 Base64 形式放进初始化响应的 `x-amz-checksum-sha256` 头并参与签名，由对象存储校验内容，不符时直接拒绝直传。
- 同一课程、同一文件的重复初始化不做去重，每次调用都创建新的 `upload_id`。

### 4.3 初始化上传

```http
POST /api/v1/courses/{course_id}/materials/uploads
Authorization: Bearer <access_token>
```

请求 Schema `MaterialUploadInitRequest`：

| 字段 | 类型 | 必填 | 规则 |
| --- | --- | --- | --- |
| `filename` | string | 是 | 4.2 的文件名与扩展名规则 |
| `content_type` | string | 是 | 4.2 表中的规范 MIME，且必须与扩展名匹配 |
| `size` | integer | 是 | 1 – `MATERIAL_MAX_UPLOAD_BYTES`（默认 52 428 800） |
| `sha256` | string | 是 | 64 位十六进制字符串 |

请求体拒绝未声明字段和显式 `null`，否则返回 `422 VALIDATION_ERROR`。

初始化请求：

```json
{
  "filename": "chapter-1.pdf",
  "content_type": "application/pdf",
  "size": 1048576,
  "sha256": "3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855e"
}
```

成功响应为 `201 Created`，Schema `MaterialUploadInitResponse`：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `upload_id` | UUID string | 上传会话 ID，供完成接口引用 |
| `upload_url` | string | 预签名上传地址 |
| `method` | string | 固定为 `PUT` |
| `headers` | object | 直传时必须逐字附带的请求头，固定为 `Content-Type`（规范 MIME）、`x-amz-checksum-sha256`（`sha256` 的 Base64）、`If-None-Match: *`；三者都参与签名，增删改任一都会导致签名不符 |
| `expires_at` | ISO 8601 UTC string | **PUT 地址的到期时间**：初始化时刻 + 10 分钟（配置 `MATERIAL_UPLOAD_URL_TTL_SECONDS`）。仅约束直传，与确认截止时间无关 |
| `confirm_deadline_at` | ISO 8601 UTC string | 完成确认的截止时间：初始化时刻 + 24 小时（配置 `MATERIAL_UPLOAD_CONFIRM_TTL_SECONDS`） |

```json
{
  "upload_id": "0f6b8b2c-6a4c-4f2e-9f9f-2a1f4c9d7e10",
  "upload_url": "https://storage.example/edu-ai/courses/70d1bdfa/materials/0f6b8b2c?X-Amz-Signature=...",
  "method": "PUT",
  "headers": {
    "Content-Type": "application/pdf"
  },
  "expires_at": "2026-09-20T08:40:00Z",
  "confirm_deadline_at": "2026-09-21T08:30:00Z"
}
```

服务端必须先在同一事务中持久化上传会话并**提交成功后**才返回 `201`；落库失败返回 `500 INTERNAL_ERROR`，且不得下发预签名地址。

### 4.4 浏览器直传约定

- 必须使用响应中的 `method`，并完整、原样发送响应 `headers` 中的三个签名头；浏览器自动附加的 `Origin`、`Content-Length` 等头不在此限制内。三个签名头的含义分别是：`Content-Type` 声明类型；`x-amz-checksum-sha256` 让对象存储校验内容与声明摘要一致，不符时 PUT 被拒；`If-None-Match: *` 是条件写入，对象已存在时 PUT 返回 `412`，因此**重复 PUT 不会被覆盖**。
- 请求体为文件原始字节流，不得使用 multipart 表单或额外包装。
- 必须发送声明的 `size` 个字节；服务端在完成接口比对实际大小。
- PUT 应在 `expires_at` 之前发起。地址过期由对象存储自行拒绝（通常 `403`），不涉及本 API 的错误码；客户端应重新初始化上传。
- `upload_url` 与 `headers` 含签名参数，前端不得持久化缓存，也不得写入日志或上报。
- 对象存储须配置允许前端来源的 `PUT` 与 `Content-Type` 头，否则浏览器预检失败。

### 4.5 完成上传

```http
POST /api/v1/courses/{course_id}/materials/uploads/{upload_id}/complete
Authorization: Bearer <access_token>
```

该接口**没有请求字段**：请求体可以省略或传空对象 `{}`；带任何未声明字段（或字段结构不合法）时返回 `422 VALIDATION_ERROR`。

服务端处理顺序固定为：认证（401）→ 课程存在（404）→ 课程教师（403）→ 课程未归档（409）→ 上传会话存在且属于本课程（404）→ **已完成则幂等返回 202** → 会话未过期（422）→ 对象确认（422/503）→ 创建资料与任务（202）。该顺序决定同时违反多条规则时的响应；幂等判定排在到期校验之前，因此重复确认不会因为确认窗口已过而失败（见 4.6）。

对象确认：服务端向存储适配器发起带 `x-amz-checksum-mode: ENABLED` 的 HeadObject，读取对象实际大小、内容类型与**存储侧记录的 SHA-256 校验值**（`x-amz-checksum-sha256`，Base64），**不使用 ETag 代替内容摘要**（ETag 对分片上传不是内容摘要）。判定规则：

| 情况 | 结果 |
| --- | --- |
| 对象不存在 | `422 UPLOAD_INVALID`（`OBJECT_MISSING`） |
| 实际大小 ≠ 初始化的 `size` | `422 UPLOAD_INVALID`（`OBJECT_SIZE_MISMATCH`） |
| 存储侧返回的内容类型 ≠ 初始化的规范 MIME | `422 UPLOAD_INVALID`（`OBJECT_TYPE_MISMATCH`） |
| 存储侧记录了校验值且 ≠ 初始化的 `sha256` | `422 UPLOAD_INVALID`（`CHECKSUM_MISMATCH`） |
| 存储侧未返回校验值 | 只记日志，不因此拒绝（部分兼容实现不返回该头）；大小与类型仍必须一致 |
| 存储配置缺失、超时或不可达 | `503 SERVICE_UNAVAILABLE`，**不创建资料与任务** |

内容与声明摘要的一致性问题在直传阶段就已由存储侧拦截（4.4），因此 `CHECKSUM_MISMATCH` 只在对象由其他途径写入时才可能出现，属防御性校验。

成功响应为 `202 Accepted`，Schema `MaterialUploadCompleteResponse`：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `material` | `MaterialDetail` | 新建或已存在的资料，见 4.7 |
| `job` | `JobStatus` | `MATERIAL_PARSE` 任务，见 4.7 |

```json
{
  "material": {
    "id": "9c2f1e77-5b3a-4d18-9c1e-6f1a2b3c4d5e",
    "course_id": "70d1bdfa-bb1a-4b22-9f13-9f1398aeb53c",
    "filename": "chapter-1.pdf",
    "content_type": "application/pdf",
    "size": 1048576,
    "status": "PROCESSING",
    "uploaded_by": "a5e675f0-696c-4970-a453-e05c85d4a9e9",
    "error_message": null,
    "created_at": "2026-09-20T08:31:00Z",
    "updated_at": "2026-09-20T08:31:00Z"
  },
  "job": {
    "id": "b1e4d2c8-7f5a-4a19-8e2d-3c6b5a4f9e01",
    "type": "MATERIAL_PARSE",
    "status": "PENDING",
    "progress": 0,
    "resource_type": "MATERIAL",
    "resource_id": "9c2f1e77-5b3a-4d18-9c1e-6f1a2b3c4d5e",
    "error": null,
    "created_at": "2026-09-20T08:31:00Z",
    "started_at": null,
    "finished_at": null
  }
}
```

资料与任务必须**在同一事务中创建**；任一失败整体回滚，不产生孤立的资料或无任务的资料。

### 4.6 重复完成与到期规则

重复完成语义：

- 首次完成：返回 `202`，创建一条资料与一个 `MATERIAL_PARSE` 任务，并把该响应保存为**完成响应快照**（附着在上传会话上）。
- 同一 `upload_id` 的**重复完成（含并发）一律返回 `202`**，响应回填首次快照：即使资料此后被删除（5.2）或解析状态已变化，返回的 `material` 与 `job` 也与首次完全相同；不创建第二条资料，也不创建第二个任务。
- 实现要求：完成操作先对上传会话行加锁（`SELECT ... FOR UPDATE`），并以该会话上的完成标记或已创建资料 ID 判定，保证并发下唯一。
- 已完成会话的重复确认**不受 24 小时确认窗口限制**；窗口只约束首次确认。
- 该接口不是“重试解析”：资料已存在时的重试解析属于第 5 节的 `POST /materials/{material_id}/parse`。

到期规则：

| 项目 | 时限 | 配置 | 过期后果 |
| --- | --- | --- | --- |
| 预签名 PUT 地址 | 10 分钟 | `MATERIAL_UPLOAD_URL_TTL_SECONDS` | 对象存储拒绝 PUT，客户端需重新初始化 |
| 完成确认窗口 | 24 小时 | `MATERIAL_UPLOAD_CONFIRM_TTL_SECONDS` | 首次确认返回 `422 UPLOAD_INVALID`（`UPLOAD_EXPIRED`） |

两个时限都自初始化时刻起算，互不影响：`expires_at` 过期后对象已直传成功，仍可在确认窗口内完成确认。

过期清理：服务端定期锁定超过确认窗口仍未确认的上传会话，删除其孤立对象并设置 `expired_at`；会话记录保留供审计，已标记的会话不重复处理。清理通过独立维护命令执行，不对外暴露接口；清理前后，过期会话的首次确认均按 `UPLOAD_EXPIRED` 拒绝。已完成的会话及其资料对象不被清理。

### 4.7 状态查询接口

两个状态查询接口返回同一份业务事实的两个视角：资料的处理状态，与解析任务的执行状态。

`MaterialDetail`：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `id` | UUID string | 资料 ID |
| `course_id` | UUID string | 所属课程 ID |
| `filename` | string | 原始文件名 |
| `content_type` | string | 规范 MIME |
| `size` | integer | 字节数 |
| `status` | `UPLOADING` / `UPLOADED` / `PROCESSING` / `READY` / `FAILED` | 解析状态；完成确认后即为 `PROCESSING`，由解析 Worker 推进（见 5.5） |
| `uploaded_by` | UUID string | 上传教师的用户 ID |
| `error_message` | string 或 `null` | 失败原因的安全描述；非 `FAILED` 时为 `null` |
| `created_at` | ISO 8601 UTC string | 创建时间 |
| `updated_at` | ISO 8601 UTC string | 最近更新时间 |

`JobStatus`（与第 10 节 `GET /jobs/{job_id}` 同一结构）：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `id` | UUID string | 任务 ID |
| `type` | `MATERIAL_PARSE` / `PRACTICE_GENERATE` / `SUBMISSION_GRADE` | 任务类型；课件上传只会产生 `MATERIAL_PARSE` |
| `status` | `PENDING` / `RUNNING` / `SUCCEEDED` / `FAILED` / `CANCELLED` | 任务状态；由解析 Worker 推进（见 5.5） |
| `progress` | integer | 0–100；`PENDING` 为 `0` |
| `resource_type` | `MATERIAL` / `PRACTICE_SET` / `SUBMISSION` | 关联资源类型 |
| `resource_id` | UUID string | 关联资源 ID（资料 ID） |
| `error` | string 或 `null` | 失败原因的安全描述；非 `FAILED` 时为 `null` |
| `created_at` | ISO 8601 UTC string | 创建时间 |
| `started_at` | ISO 8601 UTC string 或 `null` | 开始时间 |
| `finished_at` | ISO 8601 UTC string 或 `null` | 结束时间 |

接口与权限：

| 方法与路径 | 请求 | HTTP | 响应 Schema | 权限 |
| --- | --- | --- | --- | --- |
| `GET /materials/{material_id}` | 无请求体 | 200 | `MaterialDetail` | 资料所属课程的成员（教师或学生） |
| `GET /jobs/{job_id}` | 无请求体 | 200 | `JobStatus` | `MATERIAL_PARSE` 任务的可见性等同于其 `resource_id` 对应资料所属课程的成员 |

- 两个接口均无查询参数；不返回章节、大纲或知识点。
- 归档课程的资料与任务**仍可读**，返回 `200`；归档只禁止写入。
- 资料或任务不存在，或当前用户不是对应课程成员时，统一返回 `404 RESOURCE_NOT_FOUND`，不区分“不存在”与“不可见”，避免用于枚举资源。
- 这两个接口不适用 `ROLE_FORBIDDEN`：访问权完全由课程成员身份决定，教师与学生同等可读。
- 轮询建议沿用第 10 节；状态由解析 Worker 推进（见 5.5），前端应把 `PROCESSING`/`PENDING` 展示为“排队中/处理中”，不得判定为失败。

### 4.8 上传与状态查询的错误响应

所有错误沿用第 1 节的 `error` 对象与请求追踪规则。`UPLOAD_INVALID` 的 `details` 至少包含 `reason`（稳定枚举字符串）与 `field`（相关请求字段，可选）。

| 场景 | HTTP | 错误码 | `details.reason` |
| --- | --- | --- | --- |
| 缺少、无效或过期的 Access Token | 401 | `AUTH_TOKEN_EXPIRED` | — |
| 学生调用初始化或完成接口 | 403 | `ROLE_FORBIDDEN` | — |
| 教师但不是该课程的创建教师 | 403 | `COURSE_FORBIDDEN` | — |
| 课程不存在 | 404 | `RESOURCE_NOT_FOUND` | — |
| `upload_id` 不存在，或不属于路径中的课程 | 404 | `RESOURCE_NOT_FOUND` | — |
| 资料或任务不存在，或当前用户不是课程成员 | 404 | `RESOURCE_NOT_FOUND` | — |
| 对归档课程初始化或完成上传 | 409 | `COURSE_ARCHIVED` | — |
| 请求体结构不合法：缺字段、类型错误、未声明字段、显式 `null`，或路径 UUID 不合法 | 422 | `VALIDATION_ERROR` | `details.errors` 为字段级说明 |
| `filename` 扩展名不在白名单，或含 `/`、`\`、空字节、超长 | 422 | `UPLOAD_INVALID` | `FILE_TYPE_NOT_ALLOWED`（`field`: `filename`） |
| `content_type` 不是规范 MIME，或与扩展名不匹配 | 422 | `UPLOAD_INVALID` | `CONTENT_TYPE_MISMATCH`（`field`: `content_type`） |
| `size` 小于 1 或大于当前上限 | 422 | `UPLOAD_INVALID` | `SIZE_OUT_OF_RANGE`（`field`: `size`，另含 `max_size_bytes`） |
| `sha256` 不是 64 位十六进制字符串 | 422 | `UPLOAD_INVALID` | `SHA256_INVALID`（`field`: `sha256`） |
| 超过 24 小时确认窗口后首次完成 | 422 | `UPLOAD_INVALID` | `UPLOAD_EXPIRED` |
| 对象不存在（未直传或传到了其他键） | 422 | `UPLOAD_INVALID` | `OBJECT_MISSING` |
| 对象实际大小与初始化声明不一致 | 422 | `UPLOAD_INVALID` | `OBJECT_SIZE_MISMATCH` |
| 对象内容类型与初始化声明不一致 | 422 | `UPLOAD_INVALID` | `OBJECT_TYPE_MISMATCH` |
| 存储侧校验值与初始化的 `sha256` 不一致 | 422 | `UPLOAD_INVALID` | `CHECKSUM_MISMATCH` |
| 对象存储配置缺失或不可达（初始化签名或完成确认阶段） | 503 | `SERVICE_UNAVAILABLE` | `details.component`: `storage` |
| 方法不被路径支持（例如对初始化接口发 `GET`） | 405 | `METHOD_NOT_ALLOWED` | — |
| 其他未预期错误 | 500 | `INTERNAL_ERROR` | — |

字段级与语义级校验的分界：请求体的结构问题（缺字段、类型错误、未声明字段、显式 `null`、UUID 格式）一律 `VALIDATION_ERROR`；文件类型、MIME、大小、校验和、对象确认与到期等业务规则一律 `UPLOAD_INVALID`。

`UPLOAD_INVALID` 响应示例：

```json
{
  "error": {
    "code": "UPLOAD_INVALID",
    "message": "文件大小超出当前上限",
    "details": {
      "reason": "SIZE_OUT_OF_RANGE",
      "field": "size",
      "max_size_bytes": 52428800
    },
    "request_id": "uuid"
  }
}
```

## 5. 课程资料接口

| 方法 | 路径 | 说明 | 权限 | 阶段 |
| --- | --- | --- | --- | --- |
| POST | `/courses/{course_id}/materials/uploads` | 初始化上传 | 课程教师 | 已冻结，见 4.3 |
| POST | `/courses/{course_id}/materials/uploads/{upload_id}/complete` | 完成上传并创建解析任务 | 课程教师 | 已冻结，见 4.5 |
| GET | `/materials/{material_id}` | 资料详情与处理状态 | 课程成员 | 已冻结，见 4.7 |
| GET | `/courses/{course_id}/materials` | 资料列表 | 课程成员 | 已冻结，见 5.1 |
| DELETE | `/materials/{material_id}` | 删除资料 | 课程创建教师 | 已冻结，见 5.2 |
| POST | `/materials/{material_id}/parse` | 重试解析 | 课程创建教师 | 已冻结，见 5.3 |
| GET | `/materials/{material_id}/outline` | 大纲和知识点 | 课程成员 | 已冻结，见 5.4 |
| — | 解析 Worker | 独立进程，推进资料与任务状态 | 服务端内部 | 已冻结，见 5.5 |

本节接口不改变第 4 节已冻结的 `MaterialDetail` 与 `JobStatus` 结构；新增的 `MaterialOutline` 见 5.4。删除采用标记删除：已删除资料不改变 `MaterialDetail` 的字段与取值域，而是从所有读接口中消失（见 5.2）。

### 5.1 资料列表

```http
GET /api/v1/courses/{course_id}/materials?page=1&page_size=20
Authorization: Bearer <access_token>
```

查询参数：

| 参数 | 类型 | 默认 | 规则 |
| --- | --- | --- | --- |
| `page` | integer | 1 | ≥ 1 |
| `page_size` | integer | 20 | 1 – 100 |

越界（`page` < 1 或 `page_size` 不在 1–100）返回 `422 VALIDATION_ERROR`，不做静默截断。未声明字段与重复参数同样 `422 VALIDATION_ERROR`。

成功响应为 `200`，Schema `MaterialPage`（即第 1 节的分页包装，`items` 为 `MaterialDetail` 数组）：

```json
{
  "items": [
    {
      "id": "9c2f1e77-5b3a-4d18-9c1e-6f1a2b3c4d5e",
      "course_id": "70d1bdfa-bb1a-4b22-9f13-9f1398aeb53c",
      "filename": "chapter-1.pdf",
      "content_type": "application/pdf",
      "size": 1048576,
      "status": "READY",
      "uploaded_by": "a5e675f0-696c-4970-a453-e05c85d4a9e9",
      "error_message": null,
      "created_at": "2026-09-20T08:31:00Z",
      "updated_at": "2026-09-20T08:35:00Z"
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```

规则：

- 课程成员（教师或学生）均可读，含**归档课程**；归档只禁止写入。
- 按 `created_at` 倒序、同时间的按 `id` 倒序（保证同秒创建的顺序稳定）。
- **不含已删除资料**（5.2 的标记删除在查询层统一排除）。
- 课程不存在，或当前用户不是该课程成员时，统一返回 `404 RESOURCE_NOT_FOUND`，不区分“不存在”与“不可见”；本接口不适用 `ROLE_FORBIDDEN`，教师与学生同等可读。
- 不含章节、大纲或知识点；单条资料的解析状态以 `GET /materials/{material_id}` 为准。

### 5.2 删除资料

```http
DELETE /api/v1/materials/{material_id}
Authorization: Bearer <access_token>
```

无请求体；带任何请求体或查询参数不属于本契约约束（服务端忽略查询参数）。

处理顺序固定为：认证（401）→ 资料存在且未被删除（404）→ 当前用户是资料所属课程的成员（404）→ 角色（403）→ 归档（409）→ 标记删除（204）。该顺序决定同时违反多条规则时的响应。

成功响应为 `204 No Content`，无响应体。

语义：

- **仅课程的创建教师可以删除**。学生返回 `403 ROLE_FORBIDDEN`；课程的其他教师返回 `403 COURSE_FORBIDDEN`；不是课程成员的教师与学生统一 `404`（先判成员资格，后判角色）。
- **归档课程的资料不能删除**：首次删除返回 `409 COURSE_ARCHIVED`；与上传接口不同，删除没有“已归档仍可读”的例外。
- **幂等**：同一创建教师对已删除资料的再次删除返回 `204`，不报错、不重复处理。其他用户（含其他教师）对已删除资料视同不存在，统一 `404`。
- 删除是**标记删除**：记录保留（上传会话与审计依赖它），`deleted_at` 被置为当前时间；此后该资料从资料列表（5.1）、资料详情（4.7）、重试解析（5.3）与大纲查询（5.4）中消失，其 `MATERIAL_PARSE` 任务也不可再通过 `GET /jobs/{job_id}` 读取（可见性等同已删除资料 → `404`）。
- **同一事务**内完成四件事：标记 `deleted_at`（隐藏资料）→ 取消未完成解析（任务行锁内置 `CANCELLED`）→ 清空章节与知识点 → 写入**对象删除待办**。上传会话与资料行作为最小删除记录保留。
- **对象删除走独立维护命令**：待办记录关联对象的 PUT 地址过期时间；维护命令在「PUT 地址过期 + 缓冲期（`MATERIAL_DELETE_BUFFER_SECONDS`，默认 1 小时）」后删除对象并**再次核查晚到 PUT**——`If-None-Match: *` 在对象删除后放行晚到写入，因此删除后必须复查，仍有对象时保持待办继续重试，直到确认对象不再出现。删除失败（存储故障等）持续重试，不会留下永久孤立对象。
- 删除与解析互斥：`PROCESSING` 中的资料同样可以删除；Worker 回写前发现资料已删除则放弃其结果（见 5.5），运行令牌校验同时防止过期 Worker 覆盖新一轮执行。

### 5.3 重试解析

```http
POST /api/v1/materials/{material_id}/parse
Authorization: Bearer <access_token>
```

无请求字段：请求体可省略或传空对象 `{}`；带未声明字段返回 `422 VALIDATION_ERROR`。

处理顺序固定为：认证（401）→ 资料存在且未被删除（404）→ 当前用户是资料所属课程的成员（404）→ 角色（403）→ 归档（409）→ 按资料状态分流（409/202）。

成功响应为 `202 Accepted`，Schema `JobStatus`（与 4.7 同一结构，`type` 恒为 `MATERIAL_PARSE`、`resource_id` 为该资料 ID）。

按资料状态分流：

| 资料状态 | 对应任务状态 | 结果 |
| --- | --- | --- |
| `READY` | `SUCCEEDED` | `409 MATERIAL_ALREADY_READY`，不做任何修改 |
| `PROCESSING` | `PENDING` | 幂等返回 `202` 与当前任务的**原样** `JobStatus`，不重置进度与时间戳 |
| `PROCESSING` | `RUNNING` 且租约未过期 | 幂等返回 `202` 与当前任务原样 `JobStatus` |
| `PROCESSING` | `RUNNING` 但租约已过期（执行者崩溃失联） | **回收**：复用原 job ID 重置为 `PENDING` 并返回 `202`（同 `FAILED` 分支） |
| `PROCESSING` | `SUCCEEDED`（资料未 READY 的异常窗口，如 Worker 中断） | 重置后返回 `202`（同 `FAILED` 分支） |
| `FAILED` | `FAILED` | **重试**：复用原 job ID，任务重置为 `PENDING`（`progress=0`、`error=null`、`started_at=null`、`finished_at=null`、租约清除），资料状态回到 `PROCESSING` 并清空 `error_message`，返回 `202` |
| `UPLOADING` / `UPLOADED` | — | 不会通过公开接口出现（完成确认后即为 `PROCESSING`）；出现时按 `FAILED` 分支处理 |

规则：

- **仅课程的创建教师可以重试**。学生返回 `403 ROLE_FORBIDDEN`；课程的其他教师返回 `403 COURSE_FORBIDDEN`；非成员统一 `404`（顺序同 5.2）。
- **归档课程**的重试解析返回 `409 COURSE_ARCHIVED`。
- 重试**复用原任务 ID**且不创建新任务：`(type, resource_id)` 唯一约束是最终防线；应用层在任务行锁（`SELECT ... FOR UPDATE`）内完成判定与重置，并发重复调用（含 `FAILED` 资料的并发重试）只产生一次重置，其余调用按上述分流幂等返回。
- 重试不删除也不重新上传对象；Worker 从对象的 `storage_key` 重新解析。

### 5.4 大纲查询

```http
GET /api/v1/materials/{material_id}/outline
Authorization: Bearer <access_token>
```

无请求体、无查询参数。

成功响应为 `200`，Schema `MaterialOutline`：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `material_id` | UUID string | 资料 ID |
| `sections` | `MaterialSection[]` | 按 `order` **升序**排列的章节 |

`MaterialSection`：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `id` | UUID string | 章节 ID |
| `order` | integer | 从 1 开始的顺序号，连续且无重复 |
| `title` | string | 章节标题（解析产物，去除首尾空白后非空） |
| `source_type` | `PDF_PAGE` / `PPTX_SLIDE` / `DOCX_PARAGRAPH` | 来源类型，与资料的 `content_type` 对应 |
| `location_start` | integer | 起始位置：PDF 页码 / PPTX 幻灯片号 / DOCX 段落序号，**从 1 开始** |
| `location_end` | integer | 结束位置，≥ `location_start`；单页章节两者相等 |
| `knowledge_points` | `MaterialKnowledgePoint[]` | 按 `order` 升序排列的知识点 |

`MaterialKnowledgePoint`：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `id` | UUID string | 知识点 ID |
| `order` | integer | 章节内从 1 开始的顺序号 |
| `title` | string | 知识点标题 |
| `description` | string | 知识点说明 |
| `quote` | string | **可核对的原文摘录**：解析时从资料对应位置抽取的原文片段 |
| `location_start` | integer | 知识点起始位置（同章节定位规则），**从 1 开始** |
| `location_end` | integer | 知识点结束位置，≥ `location_start` |

响应示例：

```json
{
  "material_id": "9c2f1e77-5b3a-4d18-9c1e-6f1a2b3c4d5e",
  "sections": [
    {
      "id": "1d3a9c11-8f2b-4c17-9d5e-2a7b6c8d1e02",
      "order": 1,
      "title": "1.1 软件工程的定义",
      "source_type": "PDF_PAGE",
      "location_start": 3,
      "location_end": 5,
      "knowledge_points": [
        {
          "id": "6b8e2f40-1c5d-4a9b-8e7f-3d2a5c6b7e11",
          "order": 1,
          "title": "软件工程的三大要素",
          "description": "过程、方法与工具如何协同构成软件工程实践。",
          "quote": "软件工程是应用系统化、规范化的方法……",
          "location_start": 3,
          "location_end": 3
        }
      ]
    }
  ]
}
```

按资料状态分流：

| 资料状态 | 结果 |
| --- | --- |
| `READY` | `200 MaterialOutline` |
| `PROCESSING`（任务 `PENDING` / `RUNNING`） | `409 MATERIAL_NOT_READY` |
| `FAILED` | `502 AI_JOB_FAILED` |
| 不存在、已删除或当前用户不是课程成员 | `404 RESOURCE_NOT_FOUND`（统一不区分，顺序同 5.2 的读路径） |

规则：

- 课程成员（教师或学生）均可读，含**归档课程**；归档只禁止写入。
- 大纲为解析产物的持久化结果，不因重复请求重新解析；章节与知识点在解析成功时一次性落库，`order` 由服务端按解析顺序赋值。
- `502 AI_JOB_FAILED` 的 `details` 包含 `job_id`（该资料的 `MATERIAL_PARSE` 任务 ID），便于前端引导到重试入口（5.3）。

### 5.5 解析 Worker

解析 Worker 是独立于 API 进程的后台组件（独立进程/独立部署单元，通过同一数据库与对象存储协作），不对外暴露接口。职责与行为：

1. **领取**：以原子方式领取一个 `PENDING` 的 `MATERIAL_PARSE` 任务（`UPDATE ... WHERE status='PENDING' ... RETURNING` 或等价的行锁方案），置为 `RUNNING`、记录 `started_at`、`progress` 从 0 开始；同时递增执行**尝试次数**（`attempts`）、生成**运行令牌**（`run_token`）并设置**租约**（`lease_expires_at`，配置 `MATERIAL_PARSE_LEASE_SECONDS`）。
2. **解析**：按资料的 `content_type` 流式下载对象并复核大小与 SHA-256（与资料声明比对，不符即拒绝）；用 `pypdf` / `python-pptx` / `python-docx` 提取带来源位置的文本并按来源顺序分块（默认全文 120,000 字符上限、每块 8,000 字符，由 `MATERIAL_PARSE_MAX_CHARS` / `MATERIAL_PARSE_CHUNK_CHARS` 配置；超限直接进入 `FAILED`，不截断后宣称成功；扫描版 PDF 无可提取文本时明确失败，不提供 OCR）；把分块全文送入 Chat Completions 兼容端点（`AI_BASE_URL` / `AI_MODEL`），生成**同原文主要语言**的章节标题与知识点；模型输出经 Pydantic 校验，原文摘录必须能在对应来源文本中找到（找不到视为幻觉、整体无效）。
3. **成功**：章节与知识点在**同一事务**中落库并把任务置为 `SUCCEEDED`（`progress=100`、`finished_at`），资料状态置为 `READY`。资料状态与任务状态在成功路径上**允许短暂不一致**（先任务后资料或反之），读接口以资料状态为准（5.4 的分流表），不因此返回错误。
4. **失败**：任务置为 `FAILED`、资料状态置为 `FAILED`，`error` / `error_message` 写入**安全摘要**（不含堆栈、不含对象键、不含内部地址）；解析结果不落库，不产生部分章节。
5. **回写校验**：成功与失败的回写都必须携带领取时获得的运行令牌；令牌不匹配（任务已被 5.3 重置并由新一轮执行接管）或资料已被删除（5.2）时放弃回写，仅记日志。
6. **崩溃安全**：Worker 中断后任务停留于 `RUNNING` 直到租约到期；5.3 的重试对“任务 `SUCCEEDED` 但资料未 `READY`”等异常窗口同样可重置（见 5.3 分流表），不要求 Worker 自身实现租约续期。
7. **独立进程部署**：Worker 由独立进程运行（``scripts/parse_worker.py``），直接轮询数据库领取任务，不依赖 API 请求进程或内存队列；API 进程只创建 ``PENDING`` 任务。轮询间隔与批大小由部署配置决定（``WORKER_POLL_SECONDS`` / ``WORKER_BATCH_SIZE``）。

第一版不实现：扫描版 PDF 的 OCR（图片型页面按空章节处理或解析失败，失败原因写入安全摘要）；通用 `POST /jobs/{job_id}/retry`（重试一律经由 5.3，按资源类型收口）。

## 6. 课程问答接口

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/courses/{course_id}/chat-sessions` | 创建会话 | 课程成员 |
| GET | `/courses/{course_id}/chat-sessions` | 我的会话列表 | 课程成员 |
| GET | `/chat-sessions/{session_id}/messages` | 会话消息 | 会话所有者 |
| POST | `/chat-sessions/{session_id}/messages` | 发送问题 | 会话所有者 |

提问请求：

```json
{ "content": "软件生命周期包括哪些阶段？" }
```

回答响应：

```json
{
  "id": "uuid",
  "role": "ASSISTANT",
  "content": "回答正文",
  "grounded": true,
  "citations": [
    {
      "material_id": "uuid",
      "material_name": "chapter-1.pdf",
      "section_id": "uuid",
      "section_title": "1.2 软件生命周期",
      "page": 3,
      "quote": "用于展示的短引用"
    }
  ],
  "created_at": "2026-09-18T08:30:00Z"
}
```

## 7. 练习接口

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/courses/{course_id}/practice-sets/generate` | 生成练习 | 课程教师 |
| GET | `/courses/{course_id}/practice-sets` | 已发布练习 | 课程成员 |
| GET | `/practice-sets/{set_id}` | 练习详情 | 课程成员 |
| POST | `/practice-sets/{set_id}/publish` | 发布练习 | 课程教师 |
| POST | `/practice-sets/{set_id}/attempts` | 提交答案 | 学生 |
| GET | `/practice-attempts/{attempt_id}` | 答题结果 | 本人或课程教师 |

生成请求：

```json
{
  "material_ids": ["uuid"],
  "question_count": 5,
  "question_types": ["SINGLE_CHOICE", "TRUE_FALSE"],
  "difficulty": "MEDIUM"
}
```

返回 `202` 和一个 `PRACTICE_GENERATE` 任务。

## 8. 实验任务接口

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/courses/{course_id}/assignments` | 创建草稿 | 课程教师 |
| GET | `/courses/{course_id}/assignments` | 任务列表 | 课程成员 |
| GET | `/assignments/{assignment_id}` | 任务详情 | 课程成员 |
| PATCH | `/assignments/{assignment_id}` | 修改任务 | 课程教师 |
| POST | `/assignments/{assignment_id}/publish` | 发布任务 | 课程教师 |
| POST | `/assignments/{assignment_id}/close` | 关闭提交 | 课程教师 |

创建请求：

```json
{
  "title": "实验一 需求分析",
  "description": "任务说明",
  "total_score": 100,
  "due_at": "2026-09-25T15:59:00Z",
  "allow_late_submission": false,
  "rubric_items": [
    {
      "title": "需求完整性",
      "description": "功能和非功能需求是否完整",
      "max_score": 40,
      "order": 1
    },
    {
      "title": "建模规范",
      "description": "模型与文字描述是否一致",
      "max_score": 60,
      "order": 2
    }
  ]
}
```

## 9. 提交与批改接口

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/assignments/{assignment_id}/submissions/uploads` | 初始化报告上传 | 学生 |
| POST | `/assignments/{assignment_id}/submissions/uploads/{upload_id}/complete` | 完成提交 | 学生 |
| GET | `/assignments/{assignment_id}/submissions` | 提交列表 | 课程教师 |
| GET | `/submissions/{submission_id}` | 提交详情 | 本人或课程教师 |
| POST | `/submissions/{submission_id}/grade` | 触发或重试 AI 批改 | 课程教师 |
| GET | `/submissions/{submission_id}/grade-review` | 批改详情 | 教师；发布后本人 |
| PATCH | `/grade-reviews/{review_id}` | 教师修改分数和反馈 | 课程教师 |
| POST | `/grade-reviews/{review_id}/publish` | 发布正式结果 | 课程教师 |

触发批改返回 `202` 和 `SUBMISSION_GRADE` 任务。

教师修改请求：

```json
{
  "summary": "整体反馈",
  "items": [
    {
      "rubric_item_id": "uuid",
      "final_score": 35,
      "teacher_comment": "需求场景还可以补充异常流程"
    }
  ]
}
```

## 10. 异步任务接口

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| GET | `/jobs/{job_id}` | 查询任务状态 | 任务关联资源访问者 |
| POST | `/jobs/{job_id}/retry` | 重试失败任务 | 对应资源管理者 |

任务响应：

```json
{
  "id": "uuid",
  "type": "SUBMISSION_GRADE",
  "status": "RUNNING",
  "progress": 60,
  "resource_type": "SUBMISSION",
  "resource_id": "uuid",
  "error": null,
  "created_at": "2026-09-18T08:30:00Z",
  "started_at": "2026-09-18T08:30:02Z",
  "finished_at": null
}
```

任务响应结构与第 4.7 节的 `JobStatus` 一致；课件上传产生的任务为 `MATERIAL_PARSE`，`resource_type` 为 `MATERIAL`，`resource_id` 为资料 ID。

前端轮询建议：前 30 秒每 2 秒一次，之后每 5 秒一次；页面离开时停止轮询。`FAILED` 后展示后端返回的安全错误信息和重试入口（`MATERIAL_PARSE` 任务的失败重试经由 `POST /materials/{material_id}/parse`，见 5.3）。解析 Worker 的状态推进行为见 5.5。

## 11. Dashboard 接口

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| GET | `/dashboard/teacher` | 教师工作台摘要 | 教师 |
| GET | `/dashboard/student` | 学生工作台摘要 | 学生 |

Dashboard 只返回页面首屏需要的摘要和最近记录，不返回完整业务列表。

## 12. 稳定错误码

| 错误码 | HTTP | 含义 |
| --- | --- | --- |
| `AUTH_INVALID_CREDENTIALS` | 401 | 账号或密码错误 |
| `AUTH_TOKEN_EXPIRED` | 401 | 访问令牌缺失、无效或已过期 |
| `AUTH_EMAIL_TAKEN` | 409 | 注册邮箱已被占用 |
| `AUTH_TOO_MANY_ATTEMPTS` | 429 | 登录失败次数超过限流阈值 |
| `ROLE_FORBIDDEN` | 403 | 角色无权执行操作 |
| `COURSE_FORBIDDEN` | 403 | 不是课程成员或教师 |
| `COURSE_ARCHIVED` | 409 | 课程已归档，不能执行写入操作 |
| `RESOURCE_NOT_FOUND` | 404 | 资源不存在或不可见 |
| `INVITE_CODE_INVALID` | 422 | 邀请码无效 |
| `UPLOAD_INVALID` | 422 | 上传参数或对象不符：文件类型、MIME、大小、sha256、对象缺失或大小不符、超过确认窗口；`details.reason` 为稳定原因码 |
| `RUBRIC_SCORE_MISMATCH` | 422 | 评分项合计与总分不一致 |
| `MATERIAL_NOT_READY` | 409 | 资料尚未解析完成 |
| `MATERIAL_ALREADY_READY` | 409 | 资料已解析完成，无需再次解析（见 5.3） |
| `ASSIGNMENT_NOT_OPEN` | 409 | 任务未发布或已关闭 |
| `GRADE_NOT_REVIEWED` | 409 | 未完成教师复核，不能发布 |
| `AI_JOB_FAILED` | 502 | AI 或解析任务失败 |
| `VALIDATION_ERROR` | 422 | 请求体或查询参数未通过校验，`details.errors` 为字段级说明 |
| `METHOD_NOT_ALLOWED` | 405 | 请求方法不被该路径支持 |
| `INTERNAL_ERROR` | 500 | 未预期的服务端错误，响应不含异常堆栈 |
| `SERVICE_UNAVAILABLE` | 503 | 依赖未就绪（必需配置缺失、数据库或对象存储不可达）；`/health/ready` 与上传接口均可返回，上传场景 `details.component` 为 `storage` |

`details.errors` 的条目只包含 `loc`、`type`、`message`，不回显用户提交的原始值；服务端日志同样不记录密码与令牌。
