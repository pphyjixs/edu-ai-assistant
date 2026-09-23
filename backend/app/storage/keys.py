"""对象键生成规则。

契约 §4.2 与 plan 第 3 步都要求：**对象键由后端生成，不含用户文件名**。
这里的做法是让键完全由服务端可控的三段组成：

.. code-block:: text

    courses/<课程 UUID>/uploads/<上传会话 UUID><规范扩展名>

- 上传会话 UUID 是密码学随机的，键因此不可猜测；同一个课程内也不会互相覆盖；
- 扩展名只接受由类型白名单**推导**出的规范值（``.pdf`` / ``.pptx`` / ``.docx``），
  由 :func:`normalize_extension` 做格式兜底校验，用户文件名不参与拼接；
- 键中不出现原始文件名，避免路径分隔符、不可见字符与超长文件名带来的注入与兼容问题。

实验报告（契约 §9.1）复用同一套规则，但键内**必须包含任务 UUID**，
这样"同一课程、不同任务"的报告对象天然不会互相覆盖：

.. code-block:: text

    courses/<课程 UUID>/assignments/<任务 UUID>/submissions/<上传会话 UUID><规范扩展名>
"""

from __future__ import annotations

import re
import uuid

#: 对象键的顶层命名空间
OBJECT_KEY_NAMESPACE = "courses"

#: 上传对象的键模板（``{course_id}`` / ``{upload_id}`` 为 UUID，``{extension}`` 含点）
UPLOAD_KEY_TEMPLATE = (
    f"{OBJECT_KEY_NAMESPACE}/{{course_id}}/uploads/{{upload_id}}{{extension}}"
)

#: 实验报告对象的键模板；键内不含用户文件名，只由课程、任务与上传会话 UUID 拼成
SUBMISSION_KEY_TEMPLATE = (
    f"{OBJECT_KEY_NAMESPACE}/{{course_id}}/assignments/{{assignment_id}}"
    f"/submissions/{{upload_id}}{{extension}}"
)

#: 允许出现在对象键中的扩展名格式：小写字母数字，1–10 位
_EXTENSION_PATTERN = re.compile(r"^\.[a-z0-9]{1,10}$")


class InvalidExtensionError(ValueError):
    """扩展名不符合对象键规则（调用方应传入白名单推导出的规范扩展名）。"""


def normalize_extension(extension: str) -> str:
    """校验并规范化扩展名（转小写并补前导点）。

    只做**格式**兜底：真正的类型白名单在 Materials 模块的请求校验里，
    这里保证即使调用方误传原始文件名的后缀，也进不了对象键。

    :raises InvalidExtensionError: 含路径分隔符、空格或其他非法字符。
    """
    value = extension.strip().lower()
    if value and not value.startswith("."):
        value = f".{value}"
    if not _EXTENSION_PATTERN.match(value):
        raise InvalidExtensionError(f"非法的扩展名：{extension!r}")
    return value


def build_upload_object_key(
    course_id: uuid.UUID | str, upload_id: uuid.UUID | str, extension: str
) -> str:
    """构造一次上传的对象键。

    键内只含 UUID 与规范扩展名，因此可以直接用于 URL 路径（无需再转义）。
    """
    return UPLOAD_KEY_TEMPLATE.format(
        course_id=course_id,
        upload_id=upload_id,
        extension=normalize_extension(extension),
    )


def build_submission_object_key(
    course_id: uuid.UUID | str,
    assignment_id: uuid.UUID | str,
    upload_id: uuid.UUID | str,
    extension: str,
) -> str:
    """构造一次**实验报告**上传的对象键（契约 9.2）。

    键内只含课程、任务与上传会话的 UUID 以及规范扩展名，**不含用户文件名**；
    包含任务 UUID 使同一课程下不同任务的报告互不覆盖。
    """
    return SUBMISSION_KEY_TEMPLATE.format(
        course_id=course_id,
        assignment_id=assignment_id,
        upload_id=upload_id,
        extension=normalize_extension(extension),
    )
