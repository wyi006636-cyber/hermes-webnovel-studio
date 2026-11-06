"""Strict, secret-free configuration for Webnovel Writer LLM routing."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

import yaml


ROLE_NAMES = (
    "architect",
    "context_curator",
    "writer",
    "reader_editor",
)
_REMOVED_ROLE_NAMES = frozenset({"continuity_reviewer", "data_agent"})

_ROOT_KEYS = frozenset({"routing", "roles"})
_ROUTING_KEYS = frozenset({"allowed_providers", "allowed_models"})
_ROLE_KEYS = frozenset(
    {
        "route",
        "provider",
        "model",
        "temperature",
        "timeout",
        "max_tokens",
        "fallbacks",
    }
)
_TARGET_KEYS = frozenset({"provider", "model"})


class ConfigError(ValueError):
    """Raised when adapter configuration is missing, malformed, or unsafe."""


@dataclass(frozen=True)
class RouteTarget:
    """One explicit provider/model route, retaining the operator's spelling."""

    provider: str
    model: str


@dataclass(frozen=True)
class RoleConfig:
    """Validated inference settings for one fixed Webnovel role."""

    route: Literal["inherit", "configured"]
    provider: str | None
    model: str | None
    temperature: float
    timeout: float
    max_tokens: int | None
    fallbacks: tuple[RouteTarget, ...]

    @property
    def primary(self) -> RouteTarget | None:
        if self.route == "configured":
            assert self.provider is not None and self.model is not None
            return RouteTarget(self.provider, self.model)
        return None


@dataclass(frozen=True)
class WebnovelConfig:
    """Immutable, validated adapter routing configuration."""

    allowed_providers: tuple[str, ...]
    allowed_models: tuple[str, ...]
    roles: Mapping[str, RoleConfig]


_DEFAULT_CONFIG_PATH = Path(__file__).with_name("default_config.yaml")


def _load_yaml(path: Path, *, required: bool) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if not required:
            return {}
        raise ConfigError(f"required configuration file is missing: {path.name}") from None
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file: {path.name}") from exc

    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in configuration file: {path.name}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"configuration root must be an object: {path.name}")
    return loaded


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _reject_unknown(mapping: Mapping[Any, Any], allowed: frozenset[str], location: str) -> None:
    unknown = [str(key) for key in mapping if key not in allowed]
    if unknown:
        raise ConfigError(f"unknown configuration key at {location}: {unknown[0]}")


def _require_mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{location} must be an object")
    return value


def _string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{location} must be a non-empty string")
    return value.strip()


def _finite_number(value: Any, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{location} must be a finite number")
    converted = float(value)
    if not math.isfinite(converted):
        raise ConfigError(f"{location} must be a finite number")
    return converted


def _allowlist(value: Any, location: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"{location} must be a list")
    entries: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        entry = _string(raw, f"{location}[{index}]")
        if entry == "*":
            raise ConfigError(f"{location}[{index}] must name an explicit route")
        normalized = entry.casefold()
        if normalized in seen:
            raise ConfigError(f"{location} contains a duplicate entry")
        seen.add(normalized)
        entries.append(entry)
    return tuple(entries)


def _target(
    raw: Any,
    location: str,
    provider_allowlist: set[str],
    model_allowlist: set[str],
) -> RouteTarget:
    mapping = _require_mapping(raw, location)
    _reject_unknown(mapping, _TARGET_KEYS, location)
    if "provider" not in mapping or "model" not in mapping:
        raise ConfigError(f"{location} requires provider and model")
    provider = _string(mapping["provider"], f"{location}.provider")
    model = _string(mapping["model"], f"{location}.model")
    if provider.casefold() not in provider_allowlist:
        raise ConfigError(f"{location}.provider is not in routing.allowed_providers")
    if model.casefold() not in model_allowlist:
        raise ConfigError(f"{location}.model is not in routing.allowed_models")
    return RouteTarget(provider=provider, model=model)


def _role_config(
    role: str,
    raw: Any,
    provider_allowlist: set[str],
    model_allowlist: set[str],
) -> RoleConfig:
    location = f"roles.{role}"
    mapping = _require_mapping(raw, location)
    _reject_unknown(mapping, _ROLE_KEYS, location)

    route = mapping.get("route")
    if route not in ("inherit", "configured"):
        raise ConfigError(f"{location}.route must be inherit or configured")

    has_provider = "provider" in mapping
    has_model = "model" in mapping
    provider: str | None = None
    model: str | None = None
    if route == "configured":
        if not has_provider or not has_model:
            raise ConfigError(f"{location} configured route requires provider and model")
        primary = _target(
            {"provider": mapping["provider"], "model": mapping["model"]},
            location,
            provider_allowlist,
            model_allowlist,
        )
        provider, model = primary.provider, primary.model
    elif has_provider or has_model:
        raise ConfigError(f"{location} inherit route cannot specify provider or model")

    temperature = _finite_number(mapping.get("temperature"), f"{location}.temperature")
    if temperature < 0 or temperature > 2:
        raise ConfigError(f"{location}.temperature must be between 0 and 2")

    timeout = _finite_number(mapping.get("timeout"), f"{location}.timeout")
    if timeout <= 0:
        raise ConfigError(f"{location}.timeout must be greater than zero")

    raw_max_tokens = mapping.get("max_tokens")
    max_tokens: int | None
    if "max_tokens" not in mapping:
        max_tokens = None
    elif (
        isinstance(raw_max_tokens, bool)
        or not isinstance(raw_max_tokens, int)
        or raw_max_tokens <= 0
    ):
        raise ConfigError(f"{location}.max_tokens must be a positive integer")
    else:
        max_tokens = raw_max_tokens

    raw_fallbacks = mapping.get("fallbacks", [])
    if not isinstance(raw_fallbacks, list):
        raise ConfigError(f"{location}.fallbacks must be a list")
    fallbacks = tuple(
        _target(
            raw_target,
            f"{location}.fallbacks[{index}]",
            provider_allowlist,
            model_allowlist,
        )
        for index, raw_target in enumerate(raw_fallbacks)
    )

    return RoleConfig(
        route=route,
        provider=provider,
        model=model,
        temperature=temperature,
        timeout=timeout,
        max_tokens=max_tokens,
        fallbacks=fallbacks,
    )


def _validate(raw: dict[str, Any]) -> WebnovelConfig:
    _reject_unknown(raw, _ROOT_KEYS, "root")
    routing = _require_mapping(raw.get("routing"), "routing")
    roles = _require_mapping(raw.get("roles"), "roles")
    _reject_unknown(routing, _ROUTING_KEYS, "routing")
    _reject_unknown(roles, frozenset(ROLE_NAMES).union(_REMOVED_ROLE_NAMES), "roles")

    allowed_providers = _allowlist(
        routing.get("allowed_providers"), "routing.allowed_providers"
    )
    allowed_models = _allowlist(routing.get("allowed_models"), "routing.allowed_models")
    provider_allowlist = {value.casefold() for value in allowed_providers}
    model_allowlist = {value.casefold() for value in allowed_models}

    missing = [role for role in ROLE_NAMES if role not in roles]
    if missing:
        raise ConfigError(f"roles is missing required role: {missing[0]}")
    validated_roles = {
        role: _role_config(
            role,
            roles[role],
            provider_allowlist,
            model_allowlist,
        )
        for role in ROLE_NAMES
    }
    return WebnovelConfig(
        allowed_providers=allowed_providers,
        allowed_models=allowed_models,
        roles=MappingProxyType(validated_roles),
    )


PLUGIN_NAME = "webnovel-studio"


def plugin_data_root() -> Path:
    """Return the per-plugin data directory for the active Hermes profile.

    Inside Hermes this is the sanctioned ``plugin_data_dir`` (``<hermes
    home>/plugin-data/webnovel-studio``), which survives plugin updates and
    removal. Outside Hermes (unit tests, standalone use) the same layout is
    derived from ``HERMES_HOME`` or ``~/.hermes``.
    """
    try:
        from plugins.plugin_storage import plugin_data_dir
    except ModuleNotFoundError as exc:
        if exc.name not in {"plugins", "plugins.plugin_storage"}:
            raise
        configured_home = os.environ.get("HERMES_HOME")
        home = Path(configured_home).expanduser() if configured_home else Path.home() / ".hermes"
        return home / "plugin-data" / PLUGIN_NAME
    return Path(plugin_data_dir(PLUGIN_NAME)).expanduser()


def load_webnovel_config() -> WebnovelConfig:
    """Load packaged defaults plus ``<plugin data dir>/config.yaml``.

    The plugin data directory is ``<hermes home>/plugin-data/webnovel-studio``.
    The user's file is optional, deeply merged over the package default, and
    then the entire result is strictly validated before any route can be used.
    """

    defaults = _load_yaml(_DEFAULT_CONFIG_PATH, required=True)
    user_path = plugin_data_root() / "config.yaml"
    user = _load_yaml(user_path, required=False)
    return _validate(_deep_merge(defaults, user))


def build_plugin_llm_trust(config: WebnovelConfig) -> dict[str, Any]:
    """Export the exact fail-closed Hermes ``plugins.entries.*.llm`` block."""

    has_explicit_target = any(
        role.route == "configured" or bool(role.fallbacks)
        for role in config.roles.values()
    )
    return {
        "allow_provider_override": has_explicit_target,
        "allow_model_override": has_explicit_target,
        "allowed_providers": list(config.allowed_providers),
        "allowed_models": list(config.allowed_models),
        "allow_agent_id_override": False,
        "allow_profile_override": False,
    }


__all__ = [
    "ConfigError",
    "ROLE_NAMES",
    "RoleConfig",
    "RouteTarget",
    "WebnovelConfig",
    "build_plugin_llm_trust",
    "load_webnovel_config",
]
