# Edu AI Assistant

Edu AI Assistant 是一个面向高校课程教学的 AI 教学管理平台。教师可以创建课程、上传课件、发布实验任务和管理附件；学生可以学习课件、进行课程问答、练习并提交报告。系统可以调用外部 AI 模型生成课件大纲、练习内容和批改建议。

## 主要功能

- 教师与学生账号注册、登录和权限控制
- 课程创建、邀请码加课、成员管理和课程归档
- PDF、PPTX 和 DOCX 课件上传、解析、下载和删除
- 基于课件内容的课程问答与练习
- 实验任务、作业附件和学生报告管理
- AI 辅助批改、教师复核与结果发布
- 任务队列和仪表盘

## 技术栈

- 前端：React、TypeScript、Vite、Nginx
- 后端：FastAPI、Pydantic、SQLAlchemy
- 数据库：PostgreSQL 16
- 文件存储：Docker 本地持久卷
- 部署：Docker Compose

## 目录结构

```text
edu-ai-assistant/
├─ frontend/             React 前端
├─ backend/              FastAPI 后端、数据库迁移和 Worker
├─ contracts/            OpenAPI 等前后端共享契约
├─ docs/                 架构、接口和部署文档
├─ compose.yaml          Docker Compose 配置
└─ .env.docker.example   Docker 环境变量示例
```

## 使用 Docker 本地部署

### 1. 准备环境

需要安装：

- Windows：Docker Desktop（使用 Linux 容器）和 Git
- Linux：Docker Engine、Docker Compose v2 和 Git

建议至少使用 2 核 CPU、2 GB 内存。项目的 Compose 配置已限制各容器的 CPU 和内存用量。

### 2. 获取代码

```bash
git clone https://github.com/pphyjixs/edu-ai-assistant.git
cd edu-ai-assistant
git switch feat/docker-version
```

当该分支合并进 `main` 后，可省略 `git switch feat/docker-version`。

### 3. 创建环境配置

Linux/macOS：

```bash
cp .env.docker.example .env
```

Windows PowerShell：

```powershell
Copy-Item .env.docker.example .env
```

编辑 `.env`，至少修改下列配置：

```dotenv
POSTGRES_DB=edu_ai
POSTGRES_USER=edu_ai
POSTGRES_PASSWORD=使用随机生成的长密码
APP_SECRET_KEY=使用至少32字符的随机值
FRONTEND_ORIGINS=http://localhost
HTTP_PORT=80

STORAGE_BACKEND=local
STORAGE_LOCAL_ROOT=/data/uploads
STORAGE_PUBLIC_BASE_URL=
```

可使用 OpenSSL 生成随机值：

```bash
openssl rand -hex 32
```

`POSTGRES_PASSWORD` 和 `APP_SECRET_KEY` 应使用不同的随机值。`.env` 包含密钥，不要提交到 Git。

如果修改 `HTTP_PORT`，例如改为 `8080`，同时把 `FRONTEND_ORIGINS` 改为 `http://localhost:8080`。

### 4. 首次启动

先启动 PostgreSQL：

```bash
docker compose up -d --build db
```

执行数据库迁移：

```bash
docker compose run --rm backend alembic upgrade head
```

启动前端、后端和后台 Worker：

```bash
docker compose up -d --build
docker compose ps
```

默认访问地址：

```text
http://localhost/
```

首次使用时直接在页面注册教师或学生账号。

### 5. 启动、停止与查看日志

启动已创建的容器：

```bash
docker compose start
```

停止容器，保留容器和数据：

```bash
docker compose stop
```

删除容器与网络，保留数据卷：

```bash
docker compose down
```

重新创建并启动容器：

```bash
docker compose up -d
```

查看状态和日志：

```bash
docker compose ps
docker compose logs -f backend workers
```

### 6. 更新项目

```bash
git pull
docker compose build
docker compose run --rm backend alembic upgrade head
docker compose up -d
```

## 数据与上传文件

- PostgreSQL 数据保存在 `postgres_data` 持久卷。
- 课件、作业附件和学生报告保存在 `uploads_data` 持久卷。
- 重启或重建容器不会删除上述数据。
- 默认单文件上限为 1 MiB，前端和后端都会进行校验。
- 教师删除课件或作业附件时，对应实体文件与元数据会一并清理。

不要在日常停止项目时执行：

```bash
docker compose down -v
```

`-v` 会删除 PostgreSQL 和上传文件的持久卷。

## AI 配置

AI 功能需要在 `.env` 中配置外部模型服务：

```dotenv
AI_PROVIDER=
AI_API_KEY=
AI_MODEL=
AI_BASE_URL=
EMBEDDING_MODEL=
```

未配置 AI 服务时，账号、课程、文件上传等基础功能仍可使用，但课件 AI 解析、问答、练习生成和智能批改无法正常完成。

## 健康检查

```text
http://localhost/health/live
http://localhost/health/ready
```

`live` 表示后端进程正在运行，`ready` 会同时检查必要配置和数据库连接。

## 更多文档

- [Docker 服务器部署](docs/deployment-docker.md)
- [项目文档索引](docs/README.md)
- [API 契约](docs/api-contract.md)
- [OpenAPI 文件](contracts/openapi/openapi.json)
- [验收标准](docs/acceptance.md)
