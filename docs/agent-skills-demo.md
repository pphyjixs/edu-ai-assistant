# Agent Skill（Demo）

系统 Skill 位于 `backend/app/agent_skills/<name>/SKILL.md`，覆盖课件总结、生成题目、预习、复习和作业指导。Worker 首次使用时扫描系统目录；改动系统 Skill 后重启 Worker。

个人 Skill 在左下角用户菜单的「我的 Skill」上传、查看和删除。上传 `.md` 文本文件时选择功能分类。个人文件保存在 `STORAGE_LOCAL_ROOT/user-skills/<user-id>/`；Docker 现有的 `uploads_data` 卷会持久化这部分内容。删除 Skill 会删除对应目录。

文件格式示例：

```markdown
---
name: my-summary
description: 按我的习惯总结课件，突出适用条件和例题。
version: 1
---
# 总结流程

先检索目标课件，再说明主题、适用场景、核心规则和一个小例子。
```

`name` 使用小写字母、数字和连字符；`description` 不超过 300 字符；正文不超过 32 KiB。上传时只检查结构和大小，不审核内容。每个账号最多 30 份个人 Skill。

选定现有功能分类后，该账号最近上传的有效同类 Skill 作为默认版本，替代同类系统 Skill 出现在 Agent 可加载目录。选「其他」时不会自动进入目录；在首页或对话输入框的「本次 Skill」中选择，或在提问中明确说“使用 `<name>`”，才进入本次 Run。「本次 Skill」也可手选系统内置 Skill。手动选择会作为 Run 选项保存，Worker 提示模型先调用 `load_skill`。老师和学生使用各自的个人 Skill；写工具的课程教师权限仍由后端现有策略决定。

API：`GET /api/v1/users/me/skills` 返回个人与系统 Skill 列表，`POST /api/v1/users/me/skills` 上传 JSON `{ "category": "material-summary", "content": "..." }`，`DELETE /api/v1/users/me/skills/{id}` 删除。创建 Agent Run 时可在 `options.selected_skill_ids` 传入最多 3 个当前账号的 Skill ID，或在 `options.selected_skill_names` 传入最多 3 个系统 Skill 名称。
