"""上下文 Agent Run 模块。

实现依据：``docs/local-development-agent-backend.md`` 第 6 节。

与既有同步问答（``app.modules.chat``）的关系：

- **保留** `POST /chat-sessions/{id}/messages` 与历史查询接口不变，既有聊天数据与
  前端读取路径都不受影响；
- **新增** `POST /chat-sessions/{id}/runs` 与 `GET /agent-runs/{id}`：Run 只保存
  "用户消息 + 上下文标识 + 动作"，模型调用交给独立 Worker，API 数百毫秒内返回；
- 状态、进度、错误、尝试次数与租约**只存在于 jobs**（``type=AGENT_RUN``），
  ``agent_runs`` 不复制一套竞争字段。
"""
