"""集中式环境配置。

约定：

- 全部配置来自环境变量或后端目录下的 ``.env``（仅本地开发使用，不入库）。
- 代码中不硬编码任何密钥；缺少必需项时 **不在导入阶段抛错**，而是由
  :meth:`Settings.config_problems` 显式列出，交给 ``/health/ready`` 报告，
  保证实例能启动并给出可诊断的原因。
- 环境变量命名与 ``docs/deployment-vercel.md`` 第 3 节保持一致。
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

#: 后端根目录（backend/），用于定位 .env
BACKEND_DIR = Path(__file__).resolve().parents[2]

#: 生产类环境要求的 APP_SECRET_KEY 最小长度
MIN_SECRET_KEY_LENGTH = 32

#: 常见的占位值，出现在生产环境视为配置错误
PLACEHOLDER_SECRETS = frozenset(
    {
        "",
        "changeme",
        "change-me",
        "change_me",
        "secret",
        "test",
        "your-secret-key",
        "your_secret_key",
    }
)

#: 通配来源，绝不允许与凭据一起使用
WILDCARD_ORIGIN = "*"


class AppEnv(str, Enum):
    """部署环境。"""

    DEVELOPMENT = "development"
    PREVIEW = "preview"
    PRODUCTION = "production"


#: 每个环境必须提供的配置项（缺失时 /health/ready 返回 503）。
#: 取值使用环境变量的原始名字，便于直接定位到 Vercel 配置项。
#: 开发环境也要求 APP_SECRET_KEY：签发访问令牌必须有签名密钥，
#: 留空会导致登录接口在运行时才失败。
REQUIRED_SETTINGS: dict[AppEnv, tuple[str, ...]] = {
    AppEnv.DEVELOPMENT: ("APP_SECRET_KEY", "DATABASE_URL"),
    AppEnv.PREVIEW: ("APP_SECRET_KEY", "DATABASE_URL", "FRONTEND_ORIGINS"),
    AppEnv.PRODUCTION: ("APP_SECRET_KEY", "DATABASE_URL", "FRONTEND_ORIGINS"),
}


class Settings(BaseSettings):
    """运行时配置快照。所有字段都有安全默认值，缺失情况由校验方法报告。"""

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ----------------------------- 基础 -----------------------------
    app_env: AppEnv = AppEnv.DEVELOPMENT
    app_name: str = "edu-ai-assistant-api"
    app_version: str = "0.1.0"
    api_v1_prefix: str = "/api/v1"
    log_level: str = "INFO"

    # ----------------------------- 安全 -----------------------------
    app_secret_key: str = ""
    #: Access Token 有效期（秒），契约固定 3600
    access_token_expire_seconds: int = 3600
    #: Refresh Token 有效期（秒），契约固定 7 天且不轮换
    refresh_token_expire_seconds: int = 7 * 24 * 3600

    # --------------------------- 登录失败限流 ---------------------------
    #: 失败次数统计窗口（秒）；默认 15 分钟
    login_rate_limit_window_seconds: int = 15 * 60
    #: 窗口内允许的最大失败次数，达到后拒绝该规范化邮箱的登录
    login_rate_limit_max_failures: int = 5

    # ----------------------------- CORS -----------------------------
    #: 逗号分隔的允许来源；开发默认放行 Vite 常用端口
    frontend_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    #: 可选的预览域名正则，例如
    #: ``^https://edu-ai-frontend-[a-z0-9-]+\.vercel\.app$``
    frontend_origin_regex: str = ""

    # ---------------------------- 数据库 ----------------------------
    database_url: str = ""
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_connect_timeout_seconds: float = 5.0
    #: /health/ready 探测数据库的超时上限，必须远小于平台探针超时
    db_ready_timeout_seconds: float = 3.0
    db_echo: bool = False

    # --------------------- 对象存储（浏览器直传）---------------------
    #: S3 兼容服务地址，例如 ``http://127.0.0.1:9000``（MinIO）或云厂商 endpoint
    storage_endpoint: str = ""
    storage_bucket: str = ""
    storage_access_key: str = ""
    storage_secret_key: str = ""
    #: SigV4 签名区域；多数自建/兼容服务不校验取值，但参与签名计算
    storage_region: str = "us-east-1"
    #: 使用 path-style 寻址（MinIO 与多数自建服务需要），云端可用 virtual-host
    storage_path_style: bool = True
    #: 建立连接的超时（秒）；对象存储不可达时必须快速失败，不能拖住请求
    storage_connect_timeout_seconds: float = 3.0
    #: 读取响应的超时（秒）
    storage_read_timeout_seconds: float = 10.0
    #: 预签名 PUT 地址有效期（秒），契约 4.6 固定 10 分钟
    storage_upload_url_ttl_seconds: int = 10 * 60

    # ------------------------ 课件上传（Materials）-------------------
    #: 单文件大小上限（字节），契约 4.2 默认 50 MiB
    material_max_upload_bytes: int = 50 * 1024 * 1024
    #: 上传确认窗口（秒），契约 4.6 固定 24 小时
    material_upload_confirm_ttl_seconds: int = 24 * 3600
    #: Worker 租约时长（秒）：RUNNING 超过租约视为执行者失联，
    #: 回写须携带匹配的运行令牌
    material_parse_lease_seconds: int = 300
    #: 对象删除待办的缓冲期（秒）：原 PUT 地址过期后再等该时长才真正删除对象，
    #: 覆盖晚到 PUT 的重建窗口（契约 5.2）
    material_delete_buffer_seconds: int = 3600
    #: 送入模型的全文上限（字符）：超过直接 FAILED，不截断后宣称成功（契约 5.5）
    material_parse_max_chars: int = 120_000
    #: 送入模型的单块文本上限（字符）：全文按来源顺序切分为多块
    material_parse_chunk_chars: int = 8_000

    # ---------------------- 课程练习（Practice）---------------------
    #: 练习生成任务的租约（秒）；崩溃后超过租约的 RUNNING 任务可被重试回收
    practice_generate_lease_seconds: int = 300
    #: 送入模型的片段上下文上限（字符），按资料顺序轮询截断
    practice_generate_max_chars: int = 60_000

    # ------------------------------ AI ------------------------------
    ai_provider: str = ""
    ai_api_key: str = ""
    ai_model: str = ""
    embedding_model: str = ""
    #: Chat Completions 兼容端点（如 https://api.openai.com/v1）；
    #: 解析 Worker 用它生成章节标题与知识点（契约 5.5）。为空时 Worker 领取后
    #: 直接以「解析失败（模型未配置）」进入 FAILED，而不是崩溃或无限重试
    ai_base_url: str = ""
    #: 模型请求超时（秒）
    ai_timeout_seconds: float = 60.0

    # --------------------------- 异步任务 ---------------------------
    job_callback_secret: str = ""

    # ---------------------------- 派生值 ----------------------------
    @property
    def is_development(self) -> bool:
        return self.app_env is AppEnv.DEVELOPMENT

    @property
    def is_production(self) -> bool:
        return self.app_env is AppEnv.PRODUCTION

    @property
    def cors_origins(self) -> list[str]:
        """解析并规范化允许的前端来源列表。"""
        origins: list[str] = []
        for raw in self.frontend_origins.split(","):
            origin = raw.strip().rstrip("/")
            if origin and origin not in origins:
                origins.append(origin)
        if WILDCARD_ORIGIN in origins:
            raise ValueError(
                "FRONTEND_ORIGINS 不允许使用通配符 '*'：需要随请求携带凭据的 CORS "
                "必须限定为明确来源，请填写正式前端域名或使用 FRONTEND_ORIGIN_REGEX。"
            )
        return origins

    @property
    def cors_origin_regex(self) -> str | None:
        """预览域名白名单正则；未配置时返回 ``None``。"""
        regex = self.frontend_origin_regex.strip()
        return regex or None

    # ---------------------------- 校验 ------------------------------
    def config_problems(self) -> list[str]:
        """返回面向运维的配置缺失/不安全说明；返回空列表表示配置齐备。

        结果可直接放进错误响应的 ``details``，不包含任何密钥取值。
        """
        problems: list[str] = []
        for name in REQUIRED_SETTINGS[self.app_env]:
            if not getattr(self, name.lower(), "").strip():
                problems.append(f"{name} 未配置（{self.app_env.value} 环境必需）")

        if self.app_env is not AppEnv.DEVELOPMENT:
            secret = self.app_secret_key.strip()
            if secret.lower() in PLACEHOLDER_SECRETS or len(secret) < MIN_SECRET_KEY_LENGTH:
                problems.append(
                    f"APP_SECRET_KEY 不安全：{self.app_env.value} 环境需使用长度不少于 "
                    f"{MIN_SECRET_KEY_LENGTH} 字符的随机值，且不得使用占位值"
                )
        return problems


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程内唯一配置实例。

    通过 ``app.dependency_overrides[get_settings]`` 可在测试中替换。
    """
    return Settings()
