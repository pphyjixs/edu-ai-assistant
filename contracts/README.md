# Contracts

该目录存放可机器验证的前后端契约。

- `openapi`：由 FastAPI 导出的 OpenAPI JSON 或 YAML。
- `generated`：根据 OpenAPI 生成的 TypeScript 类型和客户端定义。

生成文件禁止手工编辑。接口评审先更新后端 Schema 和 `docs/api-contract.md`，再重新导出和生成。
