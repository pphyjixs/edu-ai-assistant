# Edu AI Assistant

面向高校课程教学的 AI 教学管理平台。第一版本围绕一条完整闭环实现：教师上传课程资料并创建实验任务，学生基于资料学习、提问和提交报告，AI 按评分点生成可解释的批改建议，教师复核后发布结果。

## 技术栈

- 前端：React、TypeScript、Vite
- 后端：FastAPI、Pydantic、SQLAlchemy
- 数据库：PostgreSQL
- 文件存储：兼容 S3 的对象存储
- 部署：Vercel

## 仓库目录

```text
edu-ai-assistant/
├─ frontend/             React 前端
├─ backend/              FastAPI 后端
├─ contracts/            前后端共享契约与生成产物
├─ docs/                 架构、接口、协作和验收文档
└─ scripts/              本地开发和 CI 辅助脚本
```

## 开发入口

- [项目文档索引](docs/README.md)：架构、模块分工与协作规范。
- [后端部署与本地开发](docs/deployment-vercel.md)：环境变量、PostgreSQL 和独立迁移步骤。
- [API 契约](docs/api-contract.md)与 [OpenAPI 导出物](contracts/openapi/openapi.json)。
- [验收标准](docs/acceptance.md)：第 12 节列出后端测试命令、测试库规则和用例覆盖。

后端代码位于 `backend/`，依赖清单位于该目录。真实环境配置不进入版本库，
请从 `backend/.env.example` 创建本地配置。Preview 部署验收尚未完成。

# 测试账号

教师 ： teacher-local@example.com   Local dev password 2026!

学生  ： student-local@example.com Local dev password 2026!
