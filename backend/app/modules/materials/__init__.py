"""Materials 模块：课件上传协议、资料元数据与解析任务。

本阶段（第一版第 2 步）只落地持久化模型与迁移：

- ``material_upload_sessions``：上传会话，保存课程、发起教师、随机对象键、
  预期大小、类型、哈希、两个期限与完成结果；
- ``materials``：资料，``upload_id`` 唯一约束保证一次上传只产生一条资料；
- 解析任务落在 ``app.modules.jobs`` 的 ``jobs`` 表，靠 ``(type, resource_id)``
  唯一约束保证一条资料只对应一个 ``MATERIAL_PARSE`` 任务。

请求与响应字段以 ``docs/api-contract.md`` 第 4 节为准；
业务规则见 ``docs/modules.md`` 第 4 节。
"""

__all__: list[str] = []
