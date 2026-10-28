import sys

from .cli import main


def _force_utf8() -> None:
    """Windows 控制台默认编码是 GBK，中文输出会变成乱码。

    这里统一切到 UTF-8，用户不必再手动设 PYTHONIOENCODING。
    个别环境不支持 reconfigure 时静默跳过，不影响主流程。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


if __name__ == "__main__":
    _force_utf8()
    raise SystemExit(main())
