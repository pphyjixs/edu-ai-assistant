# 多人协作规范

## 1. 推荐分工

| 负责人 | 范围 | 主要目录 |
| --- | --- | --- |
| 前端基础 | 路由、布局、HTTP 客户端、登录态、通用组件 | `frontend/src/app`、`services`、`components` |
| 前端业务 | 课程、资料、学习、任务、批改页面 | `frontend/src/features` |
| 后端基础 | 配置、认证、数据库、日志、异常、部署入口 | `backend/app/core`、`db`、`api` |
| 后端业务 | 课程、资料、任务、提交、批改 | `backend/app/modules` |
| AI 能力 | 解析、检索、问答、题目生成和批改 Schema | `backend/app/ai`、`workers` |
| 测试与交付 | 契约测试、端到端验收、部署和演示数据 | `backend/tests`、前端测试、`scripts`、`docs` |

团队人数较少时可以合并负责人，但不要让同一个需求在没有接口约定的情况下同时修改前后端。

## 2. 开发顺序

1. 后端负责人确认请求和响应 Schema。
2. 更新 `docs/api-contract.md`，评审后冻结本次接口。
3. 后端实现接口和测试；前端使用 mock 数据并行开发。
4. 后端导出 OpenAPI，生成 TypeScript 类型。
5. 前端切换真实接口并完成联调。
6. 按 `docs/acceptance.md` 执行验收。

## 3. 分支和提交

分支命名：

- `feat/frontend-material-upload`
- `feat/backend-course-api`
- `fix/grading-score-validation`
- `docs/api-contract`

提交消息推荐 Conventional Commits：

- `feat(materials): add upload completion endpoint`
- `fix(auth): reject expired refresh token`
- `test(grading): cover teacher publish workflow`
- `docs(api): document job retry response`

一个 Pull Request 只处理一个可独立验收的主题。禁止把大规模格式化、重命名和业务功能混在同一个 PR。

## 4. 接口变更规则

以下属于破坏性变更，必须先在团队中确认：

- 删除或重命名字段、接口。
- 改变字段类型或可空性。
- 改变状态枚举。
- 改变权限或业务状态流转。
- 将同步接口改为异步接口，或反之。

新增可选字段一般为兼容变更，但仍需更新契约和生成类型。

前端不得手写一份与后端重复的 API DTO。稳定后应从 OpenAPI 生成类型，生成文件禁止人工修改。

导出与生成（后端改完接口后必须重新导出并随 PR 提交）：

```powershell
# 在 backend 目录下导出契约（不需要数据库连接）
..\.venv\Scripts\python.exe scripts\export_openapi.py
# → contracts/openapi/openapi.json
```

```powershell
# 在仓库根目录生成前端类型（由前端负责人执行）
npx openapi-typescript contracts/openapi/openapi.json -o contracts/generated/api-types.ts
```

`contracts/generated` 下的文件是产物，禁止人工编辑；接口变更后由前端重新生成。

## 5. 模块所有权与越界

- 模块负责人负责该目录的设计一致性和评审。
- 通用组件只有出现两个以上真实复用场景时才抽取。
- 禁止通过复制代码绕过模块边界。
- 跨模块数据库操作必须由相关模块共同评审。
- AI 提示词改变可能影响业务输出 Schema，应与对应业务负责人共同评审。

## 6. Pull Request 检查表

- [ ] 需求关联的验收项已经列出。
- [ ] 新增或修改逻辑有测试。
- [ ] API、状态和错误码与契约一致。
- [ ] 数据库变更包含迁移。
- [ ] 没有提交密钥、真实用户数据或大文件。
- [ ] UI 包含加载、空数据和错误状态。
- [ ] 权限同时在前端和后端处理，后端为最终边界。
- [ ] 文档已同步更新。
- [ ] 本地检查和相关测试通过。

本地检查命令（在 `backend` 目录下执行）：

```powershell
..\.venv\Scripts\python.exe -m pytest -q                  # 单元 + 集成 + 契约
..\.venv\Scripts\python.exe scripts\export_openapi.py      # 接口有改动时重新导出契约
```

集成与契约测试需要一个可连的 PostgreSQL，只设置 `TEST_DATABASE_URL` 即可运行：
否则会自动派生专用测试库 `<开发库名>_test`，表结构由迁移创建，会话结束自动删除
（规则见 `docs/acceptance.md` 第 12 节）。没有可用数据库时这些用例自动跳过，
因此**只跑单元测试通过并不代表集成测试通过**，提交前请确认没有 skip。

## 7. 联调数据

准备可重复执行的 seed 数据：

- 一名教师、一名学生。
- 一门课程和有效邀请码。
- 一份已解析资料。
- 一组已发布练习。
- 一个含三个评分项的实验任务。
- 一份等待批改和一份已发布反馈的提交。

演示账号不得使用开发者个人密码，且不得连接生产数据。
