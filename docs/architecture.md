# 第一版本系统架构

## 1. 产品范围

第一版本只实现以下教学闭环：

1. 教师创建课程并邀请学生加入。
2. 教师上传 PPT、PDF 或 Word 课程资料。
3. 系统异步解析资料，生成课程大纲、知识点和可检索文本。
4. 学生查看大纲、完成 AI 生成练习，并基于课程资料问答。
5. 教师创建实验任务，配置评分项和分值。
6. 学生上传实验报告。
7. AI 按评分项生成建议分数、依据、问题定位和改进建议。
8. 教师调整结果并发布，学生查看反馈。

不纳入第一版本：完整教务排课、在线考试监考、直播课堂、视频生成、在线 IDE、复杂组织架构、付费系统。

## 2. 架构原则

- 单仓库：前端、后端、共享契约和文档统一版本管理。
- 领域拆分：目录按业务能力划分，不按页面或数据库表机械划分。
- 角色控制：教师与学生共用一套应用和 API，通过 RBAC 控制能力。
- AI 可追溯：问答和批改必须记录输入来源、模型结果和教师修改。
- 教师最终确认：AI 不直接发布正式成绩。
- 异步优先：文档解析、题目生成和报告批改均通过任务状态驱动。
- 外部持久化：数据库和文件存储使用外部服务，不依赖运行实例本地磁盘。

## 3. 仓库结构

```text
frontend/
├─ public/
└─ src/
   ├─ app/               路由、Provider、权限守卫和全局布局
   ├─ components/        无业务含义的通用组件
   ├─ features/
   │  ├─ auth/           登录、会话和角色
   │  ├─ courses/        课程、成员和邀请码
   │  ├─ materials/      上传、解析状态、大纲和知识点
   │  ├─ learning/       课程问答、练习和学习记录
   │  ├─ assignments/    实验任务和评分规则
   │  ├─ grading/        提交、AI 批改、教师复核和学生反馈
   │  └─ dashboard/      教师和学生首页数据聚合
   ├─ services/          HTTP 客户端、上传客户端和任务轮询
   ├─ types/             生成类型之外的前端本地类型
   ├─ hooks/             跨模块通用 Hook
   ├─ styles/            全局样式和设计令牌
   └─ test/              测试初始化和测试工具

backend/
├─ app/
│  ├─ main.py            FastAPI 应用入口
│  ├─ core/              配置、安全、日志、异常和依赖注入
│  ├─ db/                数据库会话、基类和迁移约定
│  ├─ api/               v1 路由聚合
│  ├─ modules/
│  │  ├─ auth/
│  │  ├─ users/
│  │  ├─ courses/
│  │  ├─ materials/
│  │  ├─ learning/
│  │  ├─ assignments/
│  │  ├─ grading/
│  │  └─ jobs/
│  ├─ ai/
│  │  ├─ providers/      大模型供应商适配器
│  │  ├─ retrieval/      切分、检索和引用
│  │  ├─ prompts/        版本化提示词
│  │  ├─ parsers/        PPT、PDF、Word 内容解析
│  │  └─ schemas/        AI 结构化输出模型
│  └─ workers/           可独立执行的异步任务处理器
└─ tests/
   ├─ unit/
   ├─ integration/
   └─ contract/

contracts/
├─ openapi/              后端导出的 OpenAPI 文件
└─ generated/            由 OpenAPI 生成的 TypeScript 类型
```

## 4. 后端模块内部约定

每个业务模块使用相同结构：

```text
module_name/
├─ router.py             HTTP 路由，只处理协议转换和依赖注入
├─ schemas.py            请求、响应和模块内部 DTO
├─ service.py            业务规则和事务边界
├─ repository.py         数据访问，不包含业务判断
├─ models.py             ORM 模型
├─ permissions.py        资源级权限判断
└─ tests/                模块单元测试
```

模块不应直接读取其他模块的 ORM 表。跨模块调用优先通过对方的 service 接口；确需联表查询的只读聚合放在 dashboard 或专门的 query service 中。

## 5. 前端模块内部约定

```text
feature_name/
├─ api/                  模块 API 调用和查询键
├─ components/           模块专用组件
├─ pages/                路由页面
├─ hooks/                模块 Hook
├─ model/                表单、状态和视图模型
└─ tests/                组件和交互测试
```

页面不能直接拼接 API URL；统一调用 `services/http` 和 feature 内的 API 函数。跨 feature 复用的纯展示组件才能提升到 `src/components`。

## 6. 核心数据对象

| 对象 | 关键内容 |
| --- | --- |
| User | 身份、姓名、角色、状态 |
| Course | 名称、描述、教师、邀请码、状态 |
| CourseMember | 课程、用户、课程内角色 |
| Material | 文件信息、存储键、解析状态 |
| MaterialSection | 章节、原文、顺序和来源定位 |
| KnowledgePoint | 名称、说明、关联章节 |
| ChatSession/Message | 会话、消息、引用和模型信息 |
| PracticeSet/Question | 练习、题目、答案、解析和知识点 |
| Assignment | 实验要求、截止时间和发布状态 |
| RubricItem | 评分项、说明、满分和顺序 |
| Submission | 学生文件、版本和状态 |
| GradeReview | AI 建议、教师终稿和发布状态 |
| GradeItem | 分项得分、证据、问题位置和建议 |
| AIJob | 任务类型、状态、进度、错误和结果引用 |

所有主要对象使用 UUID。时间以 UTC 存储，API 使用 ISO 8601，前端按用户时区显示。

## 7. 关键数据流

### 7.1 课程资料解析

```text
前端请求上传凭证
→ 浏览器直传对象存储
→ 前端确认上传
→ 后端创建 MATERIAL_PARSE 任务
→ Worker 解析、切分、生成大纲和知识点
→ 任务状态变为 SUCCEEDED
→ 前端刷新课程资料和大纲
```

### 7.2 课程问答

```text
学生提问
→ 权限检查
→ 从当前课程已解析资料中检索片段
→ 大模型基于片段回答
→ 保存回答、引用和模型元数据
→ 返回答案及来源定位
```

资料中没有依据时必须明确说明，不得伪造来源。

### 7.3 AI 批改

```text
学生提交报告
→ 教师或系统触发批改任务
→ 解析报告
→ 按 RubricItem 独立评估
→ 生成建议分数、证据和反馈
→ 教师复核修改
→ 发布正式结果
→ 学生查看反馈
```

AI 原始结果和教师修改后的结果均保留，以支持评审展示和问题追踪。
