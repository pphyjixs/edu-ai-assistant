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

## 4. 文件上传协议

文件不写入 Vercel 本地磁盘，采用浏览器直传对象存储：

1. 调用初始化接口取得 `upload_url`。
2. 浏览器把文件上传至该地址。
3. 调用完成接口，后端校验对象并创建业务记录。

初始化请求：

```json
{
  "filename": "chapter-1.pdf",
  "content_type": "application/pdf",
  "size": 1048576,
  "sha256": "hex-string"
}
```

初始化响应：

```json
{
  "upload_id": "uuid",
  "upload_url": "https://storage.example/upload",
  "method": "PUT",
  "headers": {},
  "expires_at": "2026-09-18T08:40:00Z"
}
```

## 5. 课程资料接口

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/courses/{course_id}/materials/uploads` | 初始化上传 | 课程教师 |
| POST | `/courses/{course_id}/materials/uploads/{upload_id}/complete` | 完成上传并创建解析任务 | 课程教师 |
| GET | `/courses/{course_id}/materials` | 资料列表 | 课程成员 |
| GET | `/materials/{material_id}` | 资料详情与处理状态 | 课程成员 |
| DELETE | `/materials/{material_id}` | 删除资料 | 课程教师 |
| POST | `/materials/{material_id}/parse` | 重试解析 | 课程教师 |
| GET | `/materials/{material_id}/outline` | 大纲和知识点 | 课程成员 |

完成上传响应为 `202`：

```json
{
  "material": {
    "id": "uuid",
    "filename": "chapter-1.pdf",
    "status": "PROCESSING"
  },
  "job": {
    "id": "uuid",
    "type": "MATERIAL_PARSE",
    "status": "PENDING"
  }
}
```

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

前端轮询建议：前 30 秒每 2 秒一次，之后每 5 秒一次；页面离开时停止轮询。`FAILED` 后展示后端返回的安全错误信息和重试入口。

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
| `UPLOAD_INVALID` | 422 | 上传未完成、类型或校验不符 |
| `RUBRIC_SCORE_MISMATCH` | 422 | 评分项合计与总分不一致 |
| `MATERIAL_NOT_READY` | 409 | 资料尚未解析完成 |
| `ASSIGNMENT_NOT_OPEN` | 409 | 任务未发布或已关闭 |
| `GRADE_NOT_REVIEWED` | 409 | 未完成教师复核，不能发布 |
| `AI_JOB_FAILED` | 502 | AI 或解析任务失败 |
| `VALIDATION_ERROR` | 422 | 请求体或查询参数未通过校验，`details.errors` 为字段级说明 |
| `METHOD_NOT_ALLOWED` | 405 | 请求方法不被该路径支持 |
| `INTERNAL_ERROR` | 500 | 未预期的服务端错误，响应不含异常堆栈 |
| `SERVICE_UNAVAILABLE` | 503 | 依赖未就绪（必需配置缺失或数据库不可达），由 `/health/ready` 返回 |

`details.errors` 的条目只包含 `loc`、`type`、`message`，不回显用户提交的原始值；服务端日志同样不记录密码与令牌。
