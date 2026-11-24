"""Application configuration, project-level reconqa.yaml config, and external tool resolution."""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RECONQA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Field(default=Path("./workspaces"))
    database_url: str = Field(default="sqlite+aiosqlite:///./workspaces/reconqa.db")
    bin_dir: Path = Field(default=Path("./bin"))
    cors_origins: str = Field(default="http://localhost:5173,http://127.0.0.1:5173")

    default_cmd_timeout: int = Field(default=300)
    http_timeout: float = Field(default=15.0)
    k6_proxy_url: str | None = Field(default=None)
    zap_api_url: str | None = Field(default=None)
    zap_api_key: str | None = Field(default=None)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def resolve_bin(self, tool_name: str) -> str | None:
        """Resolve an external CLI binary in standalone search order:
        1. RECONQA_BIN_DIR (when configured or non-default bin_dir)
        2. ~/.reconqa/bin
        3. ./bin
        4. System PATH (and current Python environment bin dir)
        """
        candidates: list[Path] = []
        env_bin_dir = os.environ.get("RECONQA_BIN_DIR")
        if env_bin_dir:
            candidates.append(Path(env_bin_dir) / tool_name)
        if self.bin_dir != Path("./bin"):
            candidates.append(self.bin_dir / tool_name)

        candidates.append(Path.home() / ".reconqa" / "bin" / tool_name)
        candidates.append(Path("./bin") / tool_name)

        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate.resolve())

        venv_candidate = Path(sys.executable).resolve().parent / tool_name
        if venv_candidate.is_file() and os.access(venv_candidate, os.X_OK):
            return str(venv_candidate)

        return shutil.which(tool_name)

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


# --- Project-Level Configuration (reconqa.yaml) ---


class TargetSettings(BaseModel):
    domain: str = ""
    base_url: str = ""


class BizSuccessRule(BaseModel):
    field: str = "code"
    value: Any = 0
    path_prefix: str | None = None


class ScenarioDefinition(BaseModel):
    name: str
    start_url: str | None = None
    paths: list[str] = Field(default_factory=list)
    description: str = ""


class JavaGenConfig(BaseModel):
    package_name: str = "com.reconqa.generated"
    java_version: str = "21"
    latency_multiplier: float = 3.0


class K6PerfConfig(BaseModel):
    stages: list[dict[str, Any]] = Field(
        default_factory=lambda: [
            {"duration": "15s", "target": 10},
            {"duration": "30s", "target": 30},
            {"duration": "30s", "target": 60},
            {"duration": "15s", "target": 0},
        ]
    )
    proxy_url: str | None = None
    no_conn_reuse: bool = False


class SecurityAuditConfig(BaseModel):
    include_mutations: bool = False
    custom_secret_patterns: dict[str, str] = Field(default_factory=dict)


class ProjectConfig(BaseModel):
    """Project-level configuration loaded from reconqa.yaml (shared by CLI and Web)."""

    project_name: str = "reconqa-project"
    target: TargetSettings = Field(default_factory=TargetSettings)
    scenarios: list[ScenarioDefinition] = Field(default_factory=list)
    biz_success_rules: list[BizSuccessRule] = Field(default_factory=list)
    ignore_path_prefixes: list[str] = Field(
        default_factory=lambda: [
            "/static/",
            "/assets/",
            "/_next/static/",
            "/favicon.ico",
        ]
    )
    extra_api_prefixes: list[str] = Field(default_factory=list)
    java: JavaGenConfig = Field(default_factory=JavaGenConfig)
    k6: K6PerfConfig = Field(default_factory=K6PerfConfig)
    security: SecurityAuditConfig = Field(default_factory=SecurityAuditConfig)

    @model_validator(mode="before")
    @classmethod
    def _normalize_shorthand_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        target = dict(payload.get("target") or {})
        if "domain" in payload and not target.get("domain"):
            target["domain"] = payload.pop("domain")
        if "base_url" in payload and not target.get("base_url"):
            target["base_url"] = payload.pop("base_url")
        if target:
            payload["target"] = target
        return payload

    @field_validator("biz_success_rules", mode="before")
    @classmethod
    def _coerce_biz_rules(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, dict):
            return [value]
        return value

    def match_biz_success_rule(self, path: str = "") -> dict[str, Any] | None:
        """Return the most specific configured business-success rule for a path."""
        matched: BizSuccessRule | None = None
        best_len = -1
        for rule in self.biz_success_rules:
            prefix = rule.path_prefix or ""
            if (not prefix or path.startswith(prefix)) and len(prefix) > best_len:
                matched = rule
                best_len = len(prefix)
        if matched is None:
            return None
        return {"field": matched.field, "value": matched.value}


def load_project_config(path: Path | None = None) -> ProjectConfig:
    """Load reconqa.yaml project configuration; returns defaults when absent."""
    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path))
    else:
        candidates.extend([Path("reconqa.yaml"), Path("reconqa.yml")])

    for cfg_path in candidates:
        if cfg_path.is_file():
            raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
            if not raw:
                return ProjectConfig()
            return ProjectConfig.model_validate(raw)

    return ProjectConfig()


def write_default_project_config(path: Path) -> Path:
    """Write a starter reconqa.yaml file with generic example placeholders."""
    target_path = Path(path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    template = """# ReconQA project configuration
project_name: example-webapp

target:
  domain: api.example.com
  base_url: https://api.example.com

scenarios:
  - name: 01_browse_search
    start_url: https://api.example.com/
    paths:
      - /
    description: Initial browsing and search flow

biz_success_rules:
  - field: code
    value: 0

ignore_path_prefixes:
  - /static/
  - /assets/
  - /_next/static/
  - /favicon.ico

extra_api_prefixes:
  - /api/

java:
  package_name: com.reconqa.generated
  java_version: "21"
  latency_multiplier: 3.0

k6:
  stages:
    - duration: 15s
      target: 10
    - duration: 30s
      target: 30
    - duration: 30s
      target: 60
    - duration: 15s
      target: 0
  proxy_url: null
  no_conn_reuse: false

security:
  include_mutations: false
  custom_secret_patterns: {}
"""
    target_path.write_text(template, encoding="utf-8")
    return target_path


_DOCTOR_TOOLS: list[tuple[str, str, str, str | None]] = [
    ("subfinder", "discovery", "./scripts/install-tools.sh", None),
    ("urlfinder", "discovery", "./scripts/install-tools.sh", None),
    ("tlsx", "discovery", "./scripts/install-tools.sh", None),
    ("katana", "discovery", "./scripts/install-tools.sh", None),
    ("httpx", "discovery", "./scripts/install-tools.sh", None),
    ("nuclei", "security", "./scripts/install-tools.sh", None),
    ("k6", "performance", "https://grafana.com/docs/k6/latest/set-up/install-k6/", None),
    ("mvn", "java", "Install Maven 3.9+ or use docker/Dockerfile.java-runner", None),
    ("allure", "java", "Install Allure CLI or use docker/Dockerfile.java-runner", None),
    ("mitmdump", "recorder", "uv sync --extra recorder", "mitmproxy"),
    ("playwright", "recorder", "uv sync --extra recorder && playwright install chromium", "playwright"),
    ("schemathesis", "fuzz", "uv sync --extra fuzz", "schemathesis"),
    ("java", "java", "Install OpenJDK 21+ or use docker/Dockerfile.java-runner", None),
    ("docker", "runtime", "Install Docker Engine / Docker Desktop", None),
]


def doctor_check() -> dict[str, dict[str, Any]]:
    """Inspect availability and resolved path of all external ReconQA dependencies."""
    report: dict[str, dict[str, Any]] = {}
    for tool_name, category, install_hint, py_module in _DOCTOR_TOOLS:
        bin_path = settings.resolve_bin(tool_name)
        available = bin_path is not None
        if not available and py_module is not None:
            spec = importlib.util.find_spec(py_module)
            if spec is not None:
                available = True
                bin_path = spec.origin or f"python:{py_module}"
        report[tool_name] = {
            "available": available,
            "path": bin_path,
            "category": category,
            "install_hint": install_hint,
        }
    return report


settings = Settings()
