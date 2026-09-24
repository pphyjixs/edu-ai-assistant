# Docker 单机部署

本文面向 2 核、2 GB 内存的 Linux 服务器。Docker Engine 与 Docker Compose v2 应已安装，域名的 DNS A 记录应指向服务器。建议在主机上配置 HTTPS 反向代理（如 Caddy/Nginx）；应用容器自身监听 HTTP 80 端口。

## 组成与资源

- `frontend`：静态 Vite 构建，由 Nginx 提供，并将 `/api/` 与 `/health/` 同源转发给后端。
- `backend`：一个 Uvicorn worker，避免多进程复制 Python 应用内存。
- `workers`：一个 Python 进程轮询资料解析、练习生成、报告批改和 Agent 队列。
- `db`：PostgreSQL 16，数据保存在 Docker 持久卷 `postgres_data`。
- 课件、作业附件和学生报告保存在 Docker 持久卷 `uploads_data`；AI 模型仍使用外部 API。

Compose 为数据库、API、Worker 和前端分别设置 512 MiB、512 MiB、512 MiB、96 MiB 内存上限，并限制 CPU 配额。总上限约 1.6 GiB，为系统和 Docker 留出约 400 MiB。Worker 逐队列处理，每轮每类最多一个任务，以限制峰值内存；高负载时队列可能需要更长时间消化。

## 首次部署

在服务器克隆 GitHub 仓库并进入项目目录：

```sh
cp .env.docker.example .env
```

编辑 `.env`：填写强随机 `POSTGRES_PASSWORD` 和 `APP_SECRET_KEY`、正式域名 `FRONTEND_ORIGINS`。建议使用只含十六进制字符的数据库密码，避免连接串中的特殊字符需要 URL 编码。`APP_SECRET_KEY` 至少 32 字符，同时用于签发本地文件的短期上传/下载地址。不要把 `.env` 提交到 Git。

Docker 部署默认将课件、作业附件和学生报告的单文件上限统一设为 1 MiB（1048576 字节）。前端会在上传前提示并拦截，后端会再次校验；可通过 `MATERIAL_MAX_UPLOAD_BYTES`、`ASSIGNMENT_ATTACHMENT_MAX_UPLOAD_BYTES` 和 `SUBMISSION_MAX_UPLOAD_BYTES` 调整后端限制。

默认 `STORAGE_BACKEND=local`，浏览器通过同源 `/api/v1/storage/` 上传与下载，无需另外安装 MinIO、S3 或配置 CORS。文件保存到 `uploads_data` 持久卷，重建容器不会丢失。AI 配置可选；未配置时 AI 队列任务无法成功处理。

启动数据库、API、Worker 和前端：

```sh
docker compose up -d --build db
docker compose run --rm backend alembic upgrade head
docker compose up -d --build
docker compose ps
docker compose logs -f backend workers
```

从浏览器访问 `http://服务器地址/`。配置好主机 HTTPS 反向代理后，改用正式 HTTPS 域名，并确保 `.env` 中 `FRONTEND_ORIGINS` 与该域名完全一致。

## 更新版本

```sh
git pull
docker compose build
docker compose run --rm backend alembic upgrade head
docker compose up -d
docker image prune -f
```

迁移由运维显式执行，容器启动不会自动修改数据库结构。更新前先备份数据库和本地上传文件。

## 备份与恢复

备份 PostgreSQL：

```sh
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > edu_ai.dump
```

恢复到空数据库：

```sh
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' < edu_ai.dump
```

备份上传文件：

```sh
docker compose exec -T backend tar -C /data -czf - uploads > edu_ai_uploads.tar.gz
```

恢复上传文件（先启动 backend）：

```sh
docker compose exec -T backend tar -C /data -xzf - < edu_ai_uploads.tar.gz
```

数据库卷和文件卷都不是备份；定期把 `edu_ai.dump` 与 `edu_ai_uploads.tar.gz` 异地保存。

## 运维检查

```sh
docker compose ps
docker compose logs --tail=200 backend workers db
docker stats
```

应用健康端点为 `/health/live` 和 `/health/ready`。PostgreSQL 与上传文件都位于 Docker 卷中，不要用 `docker compose down -v`，除非明确要同时删除数据库和全部上传文件。
