"""配置加载：环境变量 + 本地 .env，不依赖第三方库。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """极简 .env 解析。已存在的环境变量优先，不会被覆盖。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    github_token: str | None
    llm_base_url: str
    llm_api_key: str | None
    llm_model: str
    cache_path: Path
    timeout: float
    proxy: str | None = None

    @property
    def has_llm(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def llm_enabled(self) -> bool:
        return self.llm_api_key is not None and self.llm_base_url != ""


def load_settings(env_file: Path | None = None) -> Settings:
    _load_dotenv(env_file or (ROOT / ".env"))
    cache = os.environ.get("DEVPRISM_CACHE") or str(ROOT / ".cache" / "devprism.sqlite3")
    # 代理优先级：DEVPRISM_PROXY > HTTPS_PROXY > HTTP_PROXY
    proxy = (
        os.environ.get("DEVPRISM_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
        or None
    )
    return Settings(
        github_token=os.environ.get("GITHUB_TOKEN") or None,
        llm_base_url=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        llm_api_key=os.environ.get("LLM_API_KEY") or None,
        llm_model=os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        cache_path=Path(cache),
        timeout=float(os.environ.get("DEVPRISM_TIMEOUT", "20")),
        proxy=proxy,
    )
