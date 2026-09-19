# Frontend

React 和 TypeScript 前端工作区。

## 模块边界

- `src/app`：应用装配、路由、权限守卫和布局。
- `src/components`：跨业务复用的纯 UI 组件。
- `src/features`：按 auth、courses、materials、learning、assignments、grading、dashboard 开发业务功能。
- `src/services`：HTTP、上传、任务轮询和统一错误处理。
- `src/types`：只存放前端视图类型；API 类型从 `contracts/generated` 生成。

实现前先阅读 `docs/api-contract.md` 和 `docs/acceptance.md`。业务页面不得直接使用 `fetch` 或硬编码后端地址。
