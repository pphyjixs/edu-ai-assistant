"""超时配置的子进程回归（修复计划步骤三）。

在子进程里运行真实 pytest（加载本项目 ``tests/conftest.py`` 的超时钩子），
验证 ``TEST_TIMEOUT_SECONDS`` 的解析、优先级与超时事件日志。每个子进程都有
独立的 ``--basetemp`` 与 ``TEST_TIMEOUT_EVENT_LOG``，结束后随之清理，
不污染工作区。
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
#: 内层用例的存放目录。**必须放在 pytest 不会收集的地方**（点开头的目录是隐藏目录，
#: 默认在 ``norecursedirs`` 里被跳过）。以前它写在 ``tests/unit/`` 下，
#: 一旦某次运行没能删掉（受限环境的删除拦截、进程被杀等），下一次全量收集就会把
#: ``test_hangs_forever`` 一起收进来并永久阻塞，最后由超时机制杀掉整个 pytest 进程——
#: 症状是"跑到一半没有汇总就没了"。放到隐藏目录后，残留文件不再影响任何一次收集。
INNER_TEST_DIR = BACKEND_DIR / "tests" / ".inner_tmp"
INNER_TEST_PATH = INNER_TEST_DIR / "_timeout_inner_tmp_test.py"

#: 内层用例：生效超时值必须等于 TEST_TIMEOUT_SECONDS（而非 PYTEST_TIMEOUT）
_INNER_EFFECTIVE = """\
def test_effective_timeout(request):
    # pytest-timeout 把解析后的最终配置缓存在 config._env_timeout
    assert request.config._env_timeout == 2.0, request.config._env_timeout
"""

_INNER_TRIVIAL = """\
def test_trivial():
    assert 1 + 1 == 2
"""

_INNER_HANG = """\
import threading


def test_hangs_forever():
    threading.Event().wait()  # 永久阻塞，依赖超时机制取消
"""


def _run_inner(
    tmp_path: Path,
    source: str,
    env_overrides: dict[str, str | None],
    *extra_args: str,
) -> subprocess.CompletedProcess[str]:
    """以项目 conftest 生效的方式在子进程运行内层用例。"""
    # 目录可能被清理掉，每次写入前确保存在（隐藏目录不会被收集）
    INNER_TEST_DIR.mkdir(parents=True, exist_ok=True)
    INNER_TEST_PATH.write_text(textwrap.dedent(source), encoding="utf-8")
    basetemp = tmp_path / "basetemp"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("TEST_TIMEOUT", "PYTEST_TIMEOUT"))
    }
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    started = time.monotonic()
    try:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(INNER_TEST_PATH),
                "--basetemp",
                str(basetemp),
                "-q",
                "--no-header",
                "-p",
                "no:warnings",
                *extra_args,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(BACKEND_DIR),
            env=env,
            timeout=180,
        )
    finally:
        elapsed = time.monotonic() - started
        assert elapsed < 120, f"子进程运行过慢：{elapsed:.1f}s"
        INNER_TEST_PATH.unlink(missing_ok=True)


def test_env_timeout_runs_without_internalerror(tmp_path: Path) -> None:
    """``TEST_TIMEOUT_SECONDS=1``：普通用例正常通过，无 INTERNALERROR。"""
    result = _run_inner(tmp_path, _INNER_TRIVIAL, {"TEST_TIMEOUT_SECONDS": "1"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "INTERNALERROR" not in result.stdout + result.stderr


def test_invalid_env_timeout_is_usage_error(tmp_path: Path) -> None:
    """非法值：pytest 以配置错误退出，输出包含变量名，无类型错误堆栈。"""
    result = _run_inner(tmp_path, _INNER_TRIVIAL, {"TEST_TIMEOUT_SECONDS": "abc"})
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "TEST_TIMEOUT_SECONDS" in output
    assert "abc" in output
    assert "INTERNALERROR" not in output
    assert "TypeError" not in output


def test_cli_timeout_overrides_invalid_env(tmp_path: Path) -> None:
    """显式 CLI 参数存在时不读取也不校验环境变量：CLI 优先。"""
    result = _run_inner(
        tmp_path,
        _INNER_TRIVIAL,
        {"TEST_TIMEOUT_SECONDS": "abc"},
        "--timeout=1",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "TEST_TIMEOUT_SECONDS" not in result.stdout + result.stderr


def test_pytest_timeout_env_still_works(tmp_path: Path) -> None:
    """``PYTEST_TIMEOUT``（pytest-timeout 原生变量）继续可用。"""
    result = _run_inner(tmp_path, _INNER_TRIVIAL, {"PYTEST_TIMEOUT": "1"})
    assert result.returncode == 0, result.stdout + result.stderr


def test_project_env_beats_pytest_timeout_env(tmp_path: Path) -> None:
    """同时设置两个环境变量时 ``TEST_TIMEOUT_SECONDS`` 优先。"""
    result = _run_inner(
        tmp_path,
        _INNER_EFFECTIVE,
        {"TEST_TIMEOUT_SECONDS": "2", "PYTEST_TIMEOUT": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_hanging_test_is_cancelled_and_logged(tmp_path: Path) -> None:
    """挂起用例被超时取消，事件日志包含 nodeid、时长、PID 与栈/报告上下文。"""
    event_log = tmp_path / "events" / "timeout-events.log"
    started = time.monotonic()
    result = _run_inner(
        tmp_path,
        _INNER_HANG,
        {
            "TEST_TIMEOUT_SECONDS": "1",
            "TEST_TIMEOUT_EVENT_LOG": str(event_log),
        },
    )
    elapsed = time.monotonic() - started

    # 超时必须生效：Windows thread 方法终止整个进程（非零退出），
    # POSIX signal 方法让该测试失败（同样非零退出）
    assert result.returncode != 0, result.stdout + result.stderr
    assert elapsed < 60, f"超时未及时生效：{elapsed:.1f}s"

    assert event_log.exists(), "超时事件日志未生成"
    content = event_log.read_text(encoding="utf-8", errors="replace")
    assert "_timeout_inner_tmp_test.py::test_hangs_forever" in content  # nodeid
    assert "pid" in content  # PID 字段存在
    assert "TEST TIMEOUT" in content
    if sys.platform == "win32":
        # Windows thread 方法：事件包含全部线程栈与退出说明
        assert "thread stacks" in content
    else:
        # POSIX signal 方法：事件包含失败报告与用时
        assert "duration" in content
