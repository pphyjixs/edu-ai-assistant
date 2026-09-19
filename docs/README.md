# 开发文档索引

| 文档 | 用途 |
| --- | --- |
| [architecture.md](architecture.md) | 系统边界、核心流程、目录设计和数据流 |
| [modules.md](modules.md) | 前后端模块职责、输入输出和禁止越界事项 |
| [api-contract.md](api-contract.md) | REST API、数据结构、错误码和异步任务协议 |
| [acceptance.md](acceptance.md) | MVP 总体验收和逐模块验收标准 |
| [collaboration.md](collaboration.md) | 分支、提交、联调、代码所有权和交付流程 |
| [deployment-vercel.md](deployment-vercel.md) | Vercel 部署拓扑、环境变量和服务约束 |

## 文档优先级

发生冲突时按以下顺序处理：

1. 已评审通过的 API 契约。
2. 模块说明中的业务规则。
3. 验收标准。
4. 具体代码实现。

修改接口、状态枚举或核心业务规则时，必须在同一个 Pull Request 中同步修改对应文档。
