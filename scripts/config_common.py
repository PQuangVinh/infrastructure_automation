"""Shared local configuration helpers for NetBox/Zabbix automation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILES = (REPO_ROOT / ".env", REPO_ROOT / "scripts" / ".env")


@dataclass(frozen=True)
class NetBoxSettings:
    url: str
    token: str
    token_type: str
    validate_certs: bool


def strip_quotes(value: str) -> str:
    return value.strip().strip("'\"")


def load_simple_env(path: Path, *, override: bool, preserve: set[str]) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in preserve:
            continue
        if override or key not in os.environ:
            os.environ[key] = strip_quotes(value)


def load_project_env(*, override: bool = True, preserve: tuple[str, ...] = ()) -> None:
    preserved = set(preserve)
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        load_dotenv = None

    for env_file in ENV_FILES:
        if not env_file.exists():
            continue
        if load_dotenv:
            before = {key: os.environ.get(key) for key in preserved}
            load_dotenv(env_file, override=override)
            for key, value in before.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        else:
            load_simple_env(env_file, override=override, preserve=preserved)


def bool_value(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.lower() in {"1", "yes", "true", "on"}


def split_token(raw_token: str, default_type: str = "Token") -> tuple[str, str]:
    token = strip_quotes(raw_token or "")
    if " " in token:
        prefix, value = token.split(None, 1)
        if prefix.lower() in {"bearer", "token"}:
            return prefix, value.strip()
    return default_type, token


def netbox_settings(*, load_env: bool = True, preserve: tuple[str, ...] = ()) -> NetBoxSettings:
    if load_env:
        load_project_env(override=True, preserve=preserve)

    url = os.getenv("NETBOX_URL") or os.getenv("NETBOX_API") or "http://192.168.80.20:8000"
    token_type_default = os.getenv("NETBOX_TOKEN_TYPE", "Token")
    token_type, token = split_token(os.getenv("NETBOX_TOKEN") or os.getenv("NETBOX_API_TOKEN") or "", token_type_default)
    return NetBoxSettings(
        url=url.rstrip("/"),
        token=token,
        token_type=token_type,
        validate_certs=bool_value(os.getenv("NETBOX_VALIDATE_CERTS"), False),
    )


def netbox_auth_header(token: str, token_type: str = "Token") -> str:
    parsed_type, parsed_token = split_token(token, token_type)
    return f"{parsed_type} {parsed_token}"
