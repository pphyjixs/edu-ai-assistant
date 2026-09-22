# Backend

FastAPI 后端工作区。

## 分层

- Router：HTTP 协议、参数和依赖注入。
- Service：业务规则和事务边界。
- Repository：数据库读写。
- AI Provider：模型、解析和检索适配。
- Worker：异步任务执行器。

Router 不直接访问数据库，Repository 不包含角色判断，业务模块不直接依赖具体 AI SDK。接口实现以 `docs/api-contract.md` 为准。
