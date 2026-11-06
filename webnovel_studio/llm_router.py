"""Role-based routing exclusively through the official bound ``ctx.llm`` API."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .config import ROLE_NAMES, RoleConfig, RouteTarget, WebnovelConfig


logger = logging.getLogger(__name__)


class InvalidRoleError(ValueError):
    """Raised before host invocation when a caller supplies an unknown role."""


class RoleLlmTrustError(RuntimeError):
    """Raised when Hermes denies a configured provider/model override."""


class RoleLlmUnavailableError(RuntimeError):
    """Raised after every explicitly configured route attempt is unavailable."""


class RoleLlmInvocationError(RuntimeError):
    """Raised for invalid caller input or an adapter/host programming error."""


class _ActualRouteMismatch(RuntimeError):
    """Internal marker for a configured call that escaped adapter allowlists."""


class RoleLlmRouter:
    """Dispatch fixed Webnovel roles through a plugin's bound official LLM facade."""

    def __init__(
        self,
        llm: Any,
        config: WebnovelConfig,
        audit_sink: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self._llm = llm
        self._config = config
        self._audit_sink = audit_sink
        self._allowed_providers = {value.casefold() for value in config.allowed_providers}

    def _role(self, role: str) -> RoleConfig:
        if role not in ROLE_NAMES:
            raise InvalidRoleError("unknown Webnovel LLM role")
        return self._config.roles[role]

    @staticmethod
    def _purpose(role: str, purpose: str | None) -> str:
        return purpose if purpose is not None else f"webnovel:{role}"

    @staticmethod
    def _targets(config: RoleConfig) -> tuple[RouteTarget | None, ...]:
        return (config.primary, *config.fallbacks)

    @staticmethod
    def _is_trust_error(exc: Exception) -> bool:
        return type(exc).__name__ == "PluginLlmTrustError"

    @staticmethod
    def _is_route_unavailable(exc: Exception) -> bool:
        if isinstance(exc, (ValueError, TypeError, AssertionError, KeyError, AttributeError)):
            return False
        if isinstance(exc, (_ActualRouteMismatch, TimeoutError, ConnectionError)):
            return True
        if type(exc).__name__ in {
            "ReadTimeout",
            "ConnectTimeout",
            "PoolTimeout",
            "ConnectError",
            "RemoteProtocolError",
            "APIConnectionError",
            "APITimeoutError",
            "RateLimitError",
            "AuthenticationError",
            "PermissionDeniedError",
            "NotFoundError",
        }:
            return True
        status_code = getattr(exc, "status_code", None)
        if status_code in {401, 403, 404, 408, 409, 429, 500, 502, 503, 504}:
            return True
        if isinstance(exc, RuntimeError):
            message = str(exc).casefold()
            return any(
                marker in message
                for marker in (
                    "unavailable",
                    "not available",
                    "not found",
                    "not configured",
                    "no llm provider",
                    "no provider",
                    "no credential",
                    "no api key",
                    "authentication",
                    "unauthorized",
                    "forbidden",
                    "rate limit",
                    "quota",
                    "timeout",
                    "timed out",
                    "connection",
                    "network",
                    "empty response",
                    "did not return",
                    "provider down",
                    "model down",
                    "404",
                    "429",
                    "503",
                )
            )
        return False

    def _event(
        self,
        *,
        role: str,
        config: RoleConfig,
        attempt: int,
        target: RouteTarget | None,
        status: str,
        error: Exception | None = None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "role": role,
            "route": config.route,
            "attempt": attempt,
            "fallback_used": attempt > 1,
            "requested_provider": target.provider if target is not None else None,
            "requested_model": target.model if target is not None else None,
            "status": status,
        }
        if error is not None:
            event["error_type"] = type(error).__name__
        return event

    def _emit(self, event: dict[str, Any]) -> None:
        if self._audit_sink is None:
            return
        try:
            self._audit_sink(dict(event))
        except Exception:
            # Audit integrations are observability only. Never replace an LLM
            # success/failure with a sink failure, and never log event payloads.
            logger.warning("Webnovel LLM audit sink failed", exc_info=True)

    @staticmethod
    def _model_matches_target(actual: str, requested: str) -> bool:
        actual_ref = actual.strip().casefold()
        requested_ref = requested.strip().casefold()
        if actual_ref == requested_ref:
            return True

        actual_namespace, separator, actual_name = actual_ref.rpartition("/")
        requested_namespace, requested_separator, requested_name = requested_ref.rpartition("/")
        if separator and requested_separator and actual_namespace != requested_namespace:
            return False
        if not separator:
            actual_name = actual_ref
        if not requested_separator:
            requested_name = requested_ref
        if not actual_name.startswith(requested_name):
            return False
        suffix = actual_name[len(requested_name) :]
        return bool(
            re.fullmatch(
                r"[-:@](?:"
                r"\d{8}|\d{4}(?:-\d{2}-\d{2})?(?:-preview)?|"
                r"\d{4}-preview|v\d+(?:[._-]\d+)*|rev\d+"
                r")",
                suffix,
            )
        )

    def _validate_actual_route(self, result: Any, target: RouteTarget | None) -> None:
        if target is None:
            return
        provider = getattr(result, "provider", None)
        model = getattr(result, "model", None)
        if (
            not isinstance(provider, str)
            or provider.casefold() not in self._allowed_providers
            or provider.casefold() != target.provider.casefold()
            or not isinstance(model, str)
            or not self._model_matches_target(model, target.model)
        ):
            raise _ActualRouteMismatch(
                "host returned a route other than the explicitly requested target"
            )

    @staticmethod
    def _extend_result_audit(result: Any, event: Mapping[str, Any]) -> None:
        host_audit = getattr(result, "audit", None)
        copied = dict(host_audit) if isinstance(host_audit, Mapping) else {}
        copied.update(
            {
                key: event[key]
                for key in (
                    "role",
                    "route",
                    "attempt",
                    "fallback_used",
                    "requested_provider",
                    "requested_model",
                )
            }
        )
        result.audit = copied

    def _sync_call(
        self,
        method_name: str,
        role: str,
        config: RoleConfig,
        request: dict[str, Any],
    ) -> Any:
        last_error: Exception | None = None
        method = getattr(self._llm, method_name)
        for attempt, target in enumerate(self._targets(config), start=1):
            kwargs = dict(request)
            kwargs.update(
                provider=target.provider if target is not None else None,
                model=target.model if target is not None else None,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                timeout=config.timeout,
            )
            try:
                result = method(**kwargs)
                self._validate_actual_route(result, target)
            except Exception as exc:
                event = self._event(
                    role=role,
                    config=config,
                    attempt=attempt,
                    target=target,
                    status="failure",
                    error=exc,
                )
                self._emit(event)
                if self._is_trust_error(exc):
                    raise RoleLlmTrustError(
                        f"Hermes LLM trust configuration rejected role {role!r}"
                    ) from exc
                if not self._is_route_unavailable(exc):
                    raise RoleLlmInvocationError(
                        f"invalid Hermes LLM invocation for role {role!r}"
                    ) from exc
                last_error = exc
                continue

            event = self._event(
                role=role,
                config=config,
                attempt=attempt,
                target=target,
                status="success",
            )
            self._extend_result_audit(result, event)
            self._emit(event)
            return result

        raise RoleLlmUnavailableError(
            f"no configured Hermes LLM route is available for role {role!r}"
        ) from last_error

    async def _async_call(
        self,
        method_name: str,
        role: str,
        config: RoleConfig,
        request: dict[str, Any],
    ) -> Any:
        last_error: Exception | None = None
        method = getattr(self._llm, method_name)
        for attempt, target in enumerate(self._targets(config), start=1):
            kwargs = dict(request)
            kwargs.update(
                provider=target.provider if target is not None else None,
                model=target.model if target is not None else None,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                timeout=config.timeout,
            )
            try:
                result = await method(**kwargs)
                self._validate_actual_route(result, target)
            except Exception as exc:
                event = self._event(
                    role=role,
                    config=config,
                    attempt=attempt,
                    target=target,
                    status="failure",
                    error=exc,
                )
                self._emit(event)
                if self._is_trust_error(exc):
                    raise RoleLlmTrustError(
                        f"Hermes LLM trust configuration rejected role {role!r}"
                    ) from exc
                if not self._is_route_unavailable(exc):
                    raise RoleLlmInvocationError(
                        f"invalid Hermes LLM invocation for role {role!r}"
                    ) from exc
                last_error = exc
                continue

            event = self._event(
                role=role,
                config=config,
                attempt=attempt,
                target=target,
                status="success",
            )
            self._extend_result_audit(result, event)
            self._emit(event)
            return result

        raise RoleLlmUnavailableError(
            f"no configured Hermes LLM route is available for role {role!r}"
        ) from last_error

    def complete(
        self,
        role: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        purpose: str | None = None,
    ) -> Any:
        """Call official ``ctx.llm.complete`` for a fixed role."""

        config = self._role(role)
        return self._sync_call(
            "complete",
            role,
            config,
            {
                "messages": messages,
                "purpose": self._purpose(role, purpose),
            },
        )

    async def acomplete(
        self,
        role: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        purpose: str | None = None,
    ) -> Any:
        """Call official ``ctx.llm.acomplete`` for a fixed role."""

        config = self._role(role)
        return await self._async_call(
            "acomplete",
            role,
            config,
            {
                "messages": messages,
                "purpose": self._purpose(role, purpose),
            },
        )

    def complete_structured(
        self,
        role: str,
        *,
        instructions: str,
        input: Sequence[Any],
        json_schema: Any = None,
        json_mode: bool = False,
        schema_name: str | None = None,
        system_prompt: str | None = None,
        purpose: str | None = None,
    ) -> Any:
        """Call official ``ctx.llm.complete_structured`` for a fixed role."""

        config = self._role(role)
        return self._sync_call(
            "complete_structured",
            role,
            config,
            {
                "instructions": instructions,
                "input": input,
                "json_schema": json_schema,
                "json_mode": json_mode,
                "schema_name": schema_name,
                "system_prompt": system_prompt,
                "purpose": self._purpose(role, purpose),
            },
        )

    async def acomplete_structured(
        self,
        role: str,
        *,
        instructions: str,
        input: Sequence[Any],
        json_schema: Any = None,
        json_mode: bool = False,
        schema_name: str | None = None,
        system_prompt: str | None = None,
        purpose: str | None = None,
    ) -> Any:
        """Call official ``ctx.llm.acomplete_structured`` for a fixed role."""

        config = self._role(role)
        return await self._async_call(
            "acomplete_structured",
            role,
            config,
            {
                "instructions": instructions,
                "input": input,
                "json_schema": json_schema,
                "json_mode": json_mode,
                "schema_name": schema_name,
                "system_prompt": system_prompt,
                "purpose": self._purpose(role, purpose),
            },
        )


__all__ = [
    "InvalidRoleError",
    "RoleLlmInvocationError",
    "RoleLlmRouter",
    "RoleLlmTrustError",
    "RoleLlmUnavailableError",
]
