"""推送前自检：确认密钥没有混进任何要提交的文件。

用法（在仓库根目录）：

    python scripts/check_secrets.py

退出码 0 = 干净，可以推；1 = 发现疑似泄露，先处理再推。

检查两件事：
  1. `.env` 里的密钥值有没有出现在别的文件里；
  2. 文件里有没有 GitHub / OpenAI 风格的密钥字面量。

只报告文件名和命中的变量名，绝不打印密钥内容。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

# 与 .gitignore 保持一致：这些目录不会被提交，不必扫描
SKIP_DIRS = {".git", ".cache", "__pycache__", ".venv", "venv", "node_modules", "dist", "build"}

# 变量名里带这些词的，按密钥处理
SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD")

PATTERNS = {
    "GitHub Token (ghp_/github_pat_)": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    "GitHub 细粒度 Token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    "OpenAI 风格密钥": re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    "私钥文件头": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}


def load_secrets() -> dict[str, str]:
    if not ENV_FILE.is_file():
        return {}
    secrets: dict[str, str] = {}
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if len(value) >= 8 and any(hint in key.upper() for hint in SECRET_HINTS):
            secrets[key] = value
    return secrets


def iter_files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path == ENV_FILE:
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        yield path


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass

    secrets = load_secrets()
    problems: list[tuple[str, str]] = []
    scanned = 0

    for path in iter_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        relative = path.relative_to(ROOT).as_posix()

        for name, value in secrets.items():
            if value in text:
                problems.append((relative, f"包含 {name} 的值"))

        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                problems.append((relative, f"疑似 {label}"))

    print(f"已检查 {scanned} 个文件")
    if not secrets:
        print("提示：未从 .env 读到密钥（文件不存在或变量为空）")

    # 额外确认 .gitignore 真的挡住了 .env
    gitignore = ROOT / ".gitignore"
    if gitignore.is_file():
        rules = {line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines()}
        if ".env" not in rules:
            problems.append((".gitignore", "没有忽略 .env，密钥会被提交"))
    else:
        problems.append((".gitignore", "文件不存在，任何文件都可能被提交"))

    if not problems:
        print("结果：干净，没有发现密钥泄漏")
        return 0

    print("\n发现疑似泄漏：")
    for relative, description in problems:
        print(f"  - {relative}：{description}")
    print("\n请删除相关内容后重试。注意：一旦推送到远端，仅删除文件是不够的，"
          "提交历史里仍然可以找回，需要改写历史或立即吊销密钥。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
