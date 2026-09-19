# edu-ai-assistant
An AI teaching assistant project teamed up by Shenzhen University students to participate in the Tecent AI Coding Innovation Competition.

## 开发入口

- [项目文档索引](docs/README.md)：架构、模块分工与协作规范。
- [后端部署与本地开发](docs/deployment-vercel.md)：环境变量、PostgreSQL 和独立迁移步骤。
- [API 契约](docs/api-contract.md)与 [OpenAPI 导出物](contracts/openapi/openapi.json)。
- [验收标准](docs/acceptance.md)：第 12 节列出后端测试命令、测试库规则和用例覆盖。

后端代码位于 `backend/`，依赖清单位于该目录。真实环境配置不进入版本库，
请从 `backend/.env.example` 创建本地配置。Preview 部署验收尚未完成。
