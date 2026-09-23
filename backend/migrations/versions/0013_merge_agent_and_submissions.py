"""把「Agent 加固」与「提交与批改」两条迁移线并成一个 head。

背景：两条工作线各自从 ``0011_agent_runs`` 出发新增了一个 ``0012``：

- ``0012_agent_hardening``（Agent 上下文加固：失败阶段码、请求指纹、依据等级、
  通用引用）；
- ``0012_submissions_grading``（提交上传与批改：提交、批改版本、评分项结果等新表）。

它们**没有重叠的 DDL**：前者只给 ``jobs`` / ``materials`` / ``agent_runs`` /
``chat_message_citations`` 加列或放开约束，后者只建新表。因此这里用 Alembic 的
标准做法——一个只声明依赖关系的**空合并迁移**，把两个 head 收敛成一个，
两边都不需要改写：任何已经跑过其中一条的环境，都能继续 ``upgrade head``。

``upgrade`` / ``downgrade`` 都不执行任何 DDL：

- ``upgrade``：两个父迁移都已应用，本身无事可做；
- ``downgrade``：Alembic 会先回退到 ``down_revision`` 的**元组**（即两条父迁移之上），
  两个父迁移的 ``downgrade`` 各自回退自己的改动；如果这里也写 DDL，
  反而会在回退时重复执行。
"""

from __future__ import annotations

# revision identifiers, used by Alembic.
revision: str = "0013_merge_agent_and_submissions"
down_revision: tuple[str, ...] | None = (
    "0012_agent_hardening",
    "0012_submissions_grading",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    """两条父迁移都已存在，合并点本身不需要任何 DDL。"""


def downgrade() -> None:
    """分离两个 head 时同样不需要 DDL（各自回退由父迁移负责）。"""
