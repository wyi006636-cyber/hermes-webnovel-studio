from __future__ import annotations

import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from webnovel_studio.config import (
    ConfigError,
    ROLE_NAMES,
    RouteTarget,
    build_plugin_llm_trust,
    load_webnovel_config,
)
from webnovel_studio.llm_router import (
    InvalidRoleError,
    RoleLlmInvocationError,
    RoleLlmRouter,
    RoleLlmTrustError,
    RoleLlmUnavailableError,
)


DEFAULT_ROLE_SETTINGS = {
    "architect": (0.5, 180.0),
    "context_curator": (0.1, 120.0),
    "writer": (0.8, 300.0),
    "reader_editor": (0.3, 180.0),
}
MESSAGES = [{"role": "user", "content": "private manuscript text"}]


def write_user_config(home: Path, text: str) -> Path:
    path = home / "webnovel-writer" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def load_from(home: Path, monkeypatch: pytest.MonkeyPatch, text: str | None = None):
    if text is not None:
        write_user_config(home, text)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return load_webnovel_config()


def configured_yaml(*, fallbacks: str = "") -> str:
    return f"""
routing:
  allowed_providers: [PrimaryProvider, BackupProvider]
  allowed_models: [PrimaryModel, BackupModel]
roles:
  writer:
    route: configured
    provider: primaryprovider
    model: primarymodel
{fallbacks}
"""


class PluginLlmTrustError(RuntimeError):
    pass


class RecordingOfficialLlm:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.effects: dict[str, list[Any]] = {
            "complete": [],
            "complete_structured": [],
            "acomplete": [],
            "acomplete_structured": [],
        }

    def queue(self, method: str, *effects: Any) -> None:
        self.effects[method].extend(effects)

    def _run(self, method: str, kwargs: dict[str, Any]) -> Any:
        self.calls.append((method, kwargs))
        effects = self.effects[method]
        if not effects:
            raise AssertionError(f"no queued effect for {method}")
        effect = effects.pop(0)
        if isinstance(effect, BaseException):
            raise effect
        return effect

    def complete(self, **kwargs: Any) -> Any:
        return self._run("complete", kwargs)

    def complete_structured(self, **kwargs: Any) -> Any:
        return self._run("complete_structured", kwargs)

    async def acomplete(self, **kwargs: Any) -> Any:
        return self._run("acomplete", kwargs)

    async def acomplete_structured(self, **kwargs: Any) -> Any:
        return self._run("acomplete_structured", kwargs)


def host_result(
    *,
    provider: str = "host-provider",
    model: str = "host-model",
    structured: bool = False,
) -> SimpleNamespace:
    usage = object()
    values: dict[str, Any] = {
        "text": "ok",
        "provider": provider,
        "model": model,
        "agent_id": "default",
        "usage": usage,
        "audit": {"host": "preserved"},
    }
    if structured:
        values.update(parsed={"answer": 1}, content_type="json")
    return SimpleNamespace(**values)


# ---------------------------------------------------------------------------
# Configuration and trust export
# ---------------------------------------------------------------------------


def test_defaults_define_exactly_four_novel_roles_with_approved_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_from(tmp_path / "isolated", monkeypatch)

    assert ROLE_NAMES == tuple(DEFAULT_ROLE_SETTINGS)
    assert tuple(config.roles) == ROLE_NAMES
    assert config.allowed_providers == ()
    assert config.allowed_models == ()
    for role, (temperature, timeout) in DEFAULT_ROLE_SETTINGS.items():
        value = config.roles[role]
        assert value.route == "inherit"
        assert value.provider is None
        assert value.model is None
        assert value.temperature == temperature
        assert value.timeout == timeout
        assert value.max_tokens is None
        assert value.fallbacks == ()


def test_removed_legacy_role_entries_are_accepted_but_not_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_from(
        tmp_path,
        monkeypatch,
        """
roles:
  continuity_reviewer:
    timeout: 91
  data_agent:
    temperature: 0.4
""",
    )

    assert tuple(config.roles) == ROLE_NAMES
    assert "continuity_reviewer" not in config.roles
    assert "data_agent" not in config.roles


def test_user_config_deep_merges_over_packaged_defaults_and_isolates_hermes_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first-home"
    second = tmp_path / "second-home"
    write_user_config(first, "roles:\n  writer:\n    timeout: 42\n")
    write_user_config(second, "roles:\n  architect:\n    temperature: 1.25\n")

    monkeypatch.setenv("HERMES_HOME", str(first))
    first_config = load_webnovel_config()
    monkeypatch.setenv("HERMES_HOME", str(second))
    second_config = load_webnovel_config()

    assert first_config.roles["writer"].timeout == 42.0
    assert first_config.roles["writer"].temperature == 0.8
    assert first_config.roles["architect"].temperature == 0.5
    assert second_config.roles["architect"].temperature == 1.25
    assert second_config.roles["writer"].timeout == 300.0


def test_unset_hermes_home_uses_dot_hermes_under_home_without_touching_real_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "fake-user"
    write_user_config(fake_home / ".hermes", "roles:\n  context_curator:\n    timeout: 77\n")
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    config = load_webnovel_config()

    assert config.roles["context_curator"].timeout == 77.0


def test_active_hermes_profile_helper_wins_over_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_home = tmp_path / "process-home"
    profile_home = tmp_path / "profile-home"
    write_user_config(process_home, "roles:\n  writer:\n    timeout: 11\n")
    write_user_config(profile_home, "roles:\n  writer:\n    timeout: 22\n")
    module = ModuleType("hermes_constants")
    module.get_hermes_home = lambda: profile_home  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_constants", module)
    monkeypatch.setenv("HERMES_HOME", str(process_home))

    config = load_webnovel_config()

    assert config.roles["writer"].timeout == 22.0


@pytest.mark.parametrize(
    "text",
    [
        "unexpected: true\n",
        "routing:\n  mystery: []\n",
        "roles:\n  unknown_role:\n    timeout: 1\n",
        "roles:\n  writer:\n    mystery: true\n",
        configured_yaml(
            fallbacks="    fallbacks:\n      - provider: BackupProvider\n        model: BackupModel\n        api_key: forbidden\n"
        ),
        "roles:\n  writer:\n    api_key: forbidden\n",
    ],
)
def test_unknown_keys_roles_and_secret_fields_are_rejected(
    text: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ConfigError):
        load_from(tmp_path, monkeypatch, text)


@pytest.mark.parametrize(
    "fragment",
    [
        "temperature: true",
        "temperature: .nan",
        "temperature: .inf",
        "temperature: -0.01",
        "temperature: 2.01",
        "timeout: false",
        "timeout: 0",
        "timeout: -.inf",
        "max_tokens: true",
        "max_tokens: null",
        "max_tokens: 1.5",
        "max_tokens: 0",
    ],
)
def test_invalid_numeric_values_are_rejected(
    fragment: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = f"roles:\n  writer:\n    {fragment}\n"
    with pytest.raises(ConfigError):
        load_from(tmp_path, monkeypatch, text)


@pytest.mark.parametrize(
    "text",
    [
        "roles:\n  writer:\n    route: configured\n",
        "roles:\n  writer:\n    route: configured\n    provider: P\n",
        "roles:\n  writer:\n    route: configured\n    model: M\n",
        "roles:\n  writer:\n    provider: P\n",
        "roles:\n  writer:\n    model: M\n",
    ],
)
def test_configured_requires_both_target_fields_and_inherit_rejects_them(
    text: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ConfigError):
        load_from(tmp_path, monkeypatch, text)


@pytest.mark.parametrize(
    "text",
    [
        "roles:\n  writer:\n    fallbacks: nope\n",
        "roles:\n  writer:\n    fallbacks: [nope]\n",
        "roles:\n  writer:\n    fallbacks: [{}]\n",
    ],
)
def test_fallbacks_must_be_a_list_of_complete_target_objects(
    text: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ConfigError):
        load_from(tmp_path, monkeypatch, text)


@pytest.mark.parametrize(
    "text",
    [
        configured_yaml().replace("primaryprovider", "NotAllowed"),
        configured_yaml().replace("primarymodel", "NotAllowed"),
        configured_yaml(
            fallbacks="    fallbacks:\n      - provider: NotAllowed\n        model: BackupModel\n"
        ),
        configured_yaml(
            fallbacks="    fallbacks:\n      - provider: BackupProvider\n        model: NotAllowed\n"
        ),
    ],
)
def test_primary_and_each_fallback_require_independent_provider_and_model_allowlisting(
    text: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ConfigError):
        load_from(tmp_path, monkeypatch, text)


def test_allowlist_matching_is_casefolded_while_original_values_are_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_from(tmp_path, monkeypatch, configured_yaml())

    assert config.allowed_providers == ("PrimaryProvider", "BackupProvider")
    assert config.allowed_models == ("PrimaryModel", "BackupModel")
    assert config.roles["writer"].provider == "primaryprovider"
    assert config.roles["writer"].model == "primarymodel"


def test_yaml_uses_safe_loader_and_rejects_python_object_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = "roles: !!python/object/apply:builtins.dict []\n"

    with pytest.raises(ConfigError):
        load_from(tmp_path, monkeypatch, text)


def test_config_dataclasses_and_role_mapping_are_immutable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_from(tmp_path, monkeypatch)

    with pytest.raises(FrozenInstanceError):
        config.roles["writer"].timeout = 1  # type: ignore[misc]
    with pytest.raises(TypeError):
        config.roles["writer"] = config.roles["architect"]  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        RouteTarget("p", "m").provider = "other"  # type: ignore[misc]


def test_build_plugin_llm_trust_enables_only_validated_configured_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = load_from(tmp_path / "configured", monkeypatch, configured_yaml())
    trust = build_plugin_llm_trust(configured)

    assert trust == {
        "allow_provider_override": True,
        "allow_model_override": True,
        "allowed_providers": ["PrimaryProvider", "BackupProvider"],
        "allowed_models": ["PrimaryModel", "BackupModel"],
        "allow_agent_id_override": False,
        "allow_profile_override": False,
    }

    inherited = load_from(tmp_path / "inherited", monkeypatch)
    assert build_plugin_llm_trust(inherited) == {
        "allow_provider_override": False,
        "allow_model_override": False,
        "allowed_providers": [],
        "allowed_models": [],
        "allow_agent_id_override": False,
        "allow_profile_override": False,
    }


# ---------------------------------------------------------------------------
# Router contract against a recording fake of official ctx.llm
# ---------------------------------------------------------------------------


def test_all_six_fixed_roles_route_through_official_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_from(tmp_path, monkeypatch)
    llm = RecordingOfficialLlm()
    expected = [host_result() for _ in ROLE_NAMES]
    llm.queue("complete", *expected)
    router = RoleLlmRouter(llm, config)

    returned = [router.complete(role, MESSAGES) for role in ROLE_NAMES]

    assert returned == expected
    assert [call[1]["purpose"] for call in llm.calls] == [
        f"webnovel:{role}" for role in ROLE_NAMES
    ]


def test_inherit_complete_calls_official_method_with_none_route_and_role_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_from(tmp_path, monkeypatch, "roles:\n  architect:\n    max_tokens: 900\n")
    llm = RecordingOfficialLlm()
    expected = host_result()
    llm.queue("complete", expected)

    actual = RoleLlmRouter(llm, config).complete("architect", MESSAGES)

    assert actual is expected
    assert llm.calls == [
        (
            "complete",
            {
                "messages": MESSAGES,
                "provider": None,
                "model": None,
                "temperature": 0.5,
                "max_tokens": 900,
                "timeout": 180.0,
                "purpose": "webnovel:architect",
            },
        )
    ]


def test_custom_purpose_and_timeout_are_passed_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_from(tmp_path, monkeypatch, "roles:\n  writer:\n    timeout: 12.5\n")
    llm = RecordingOfficialLlm()
    llm.queue("complete", host_result())

    RoleLlmRouter(llm, config).complete("writer", MESSAGES, purpose="chapter-draft")

    assert llm.calls[0][1]["timeout"] == 12.5
    assert llm.calls[0][1]["purpose"] == "chapter-draft"


def test_invalid_role_fails_before_calling_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = RecordingOfficialLlm()
    router = RoleLlmRouter(llm, load_from(tmp_path, monkeypatch))

    with pytest.raises(InvalidRoleError):
        router.complete("Writer", MESSAGES)
    assert llm.calls == []


def test_official_trust_error_never_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = PluginLlmTrustError("denied")
    config = load_from(
        tmp_path,
        monkeypatch,
        configured_yaml(
            fallbacks="    fallbacks:\n      - provider: BackupProvider\n        model: BackupModel\n"
        ),
    )
    llm = RecordingOfficialLlm()
    llm.queue("complete", error, host_result(provider="BackupProvider", model="BackupModel"))

    with pytest.raises(RoleLlmTrustError) as exc_info:
        RoleLlmRouter(llm, config).complete("writer", MESSAGES)

    assert exc_info.value.__cause__ is error
    assert len(llm.calls) == 1
    assert "private manuscript" not in str(exc_info.value)
    assert "denied" not in str(exc_info.value)


def test_non_trust_permission_error_is_invocation_error_not_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_from(
        tmp_path,
        monkeypatch,
        configured_yaml(
            fallbacks="    fallbacks:\n      - provider: BackupProvider\n        model: BackupModel\n"
        ),
    )
    llm = RecordingOfficialLlm()
    expected = host_result(provider="BackupProvider", model="BackupModel")
    error = PermissionError("local permission failure")
    llm.queue("complete", error, expected)

    with pytest.raises(RoleLlmInvocationError) as exc_info:
        RoleLlmRouter(llm, config).complete("writer", MESSAGES)

    assert exc_info.value.__cause__ is error
    assert len(llm.calls) == 1


def test_official_no_api_key_error_uses_explicit_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_from(
        tmp_path,
        monkeypatch,
        configured_yaml(
            fallbacks="    fallbacks:\n      - provider: BackupProvider\n        model: BackupModel\n"
        ),
    )
    llm = RecordingOfficialLlm()
    expected = host_result(provider="BackupProvider", model="BackupModel")
    llm.queue(
        "complete",
        RuntimeError("Provider was selected but no API key was found for it"),
        expected,
    )

    assert RoleLlmRouter(llm, config).complete("writer", MESSAGES) is expected
    assert len(llm.calls) == 2


def test_unavailable_primary_uses_explicit_fallbacks_in_order_and_audits_every_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = """
routing:
  allowed_providers: [p1, p2, p3]
  allowed_models: [m1, m2, m3]
roles:
  writer:
    route: configured
    provider: p1
    model: m1
    fallbacks:
      - {provider: p2, model: m2}
      - {provider: p3, model: m3}
"""
    config = load_from(tmp_path, monkeypatch, text)
    llm = RecordingOfficialLlm()
    llm.queue(
        "complete",
        TimeoutError("first"),
        RuntimeError("provider down"),
        host_result(provider="P3", model="M3"),
    )
    events: list[dict[str, Any]] = []

    result = RoleLlmRouter(llm, config, audit_sink=events.append).complete("writer", MESSAGES)

    assert [(call[1]["provider"], call[1]["model"]) for call in llm.calls] == [
        ("p1", "m1"),
        ("p2", "m2"),
        ("p3", "m3"),
    ]
    assert [event["status"] for event in events] == ["failure", "failure", "success"]
    assert [event["attempt"] for event in events] == [1, 2, 3]
    assert [event["fallback_used"] for event in events] == [False, True, True]
    assert result.audit["attempt"] == 3
    assert result.audit["fallback_used"] is True
    assert result.audit["requested_provider"] == "p3"
    assert result.audit["requested_model"] == "m3"


def test_fallback_exhaustion_preserves_cause_without_prompt_or_secret_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_from(
        tmp_path,
        monkeypatch,
        configured_yaml(
            fallbacks="    fallbacks:\n      - provider: BackupProvider\n        model: BackupModel\n"
        ),
    )
    llm = RecordingOfficialLlm()
    last = TimeoutError("secret=abc private manuscript text")
    llm.queue("complete", TimeoutError("private manuscript text"), last)

    with pytest.raises(RoleLlmUnavailableError) as exc_info:
        RoleLlmRouter(llm, config).complete("writer", MESSAGES)

    assert exc_info.value.__cause__ is last
    assert len(llm.calls) == 2
    assert "private manuscript" not in str(exc_info.value)
    assert "secret" not in str(exc_info.value)


def test_inherit_and_configured_routes_do_not_invent_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index, text in enumerate((None, configured_yaml())):
        home = tmp_path / str(index)
        config = load_from(home, monkeypatch, text)
        llm = RecordingOfficialLlm()
        llm.queue("complete", TimeoutError("down"))
        with pytest.raises(RoleLlmUnavailableError):
            RoleLlmRouter(llm, config).complete("writer", MESSAGES)
        assert len(llm.calls) == 1


@pytest.mark.parametrize(
    "error",
    [
        ValueError("bad input"),
        TypeError("bad call"),
        FileNotFoundError("local file missing"),
        IsADirectoryError("local path is a directory"),
    ],
)
def test_caller_errors_never_trigger_model_fallback(
    error: Exception, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_from(
        tmp_path,
        monkeypatch,
        configured_yaml(
            fallbacks="    fallbacks:\n      - provider: BackupProvider\n        model: BackupModel\n"
        ),
    )
    llm = RecordingOfficialLlm()
    llm.queue("complete", error, host_result(provider="BackupProvider", model="BackupModel"))

    with pytest.raises(RoleLlmInvocationError) as exc_info:
        RoleLlmRouter(llm, config).complete("writer", MESSAGES)

    assert exc_info.value.__cause__ is error
    assert len(llm.calls) == 1


def test_configured_actual_result_outside_adapter_allowlist_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_from(
        tmp_path,
        monkeypatch,
        configured_yaml(
            fallbacks="    fallbacks:\n      - provider: BackupProvider\n        model: BackupModel\n"
        ),
    )
    llm = RecordingOfficialLlm()
    llm.queue(
        "complete",
        host_result(provider="rogue-provider", model="PrimaryModel"),
        host_result(provider="BackupProvider", model="BackupModel"),
    )

    result = RoleLlmRouter(llm, config).complete("writer", MESSAGES)

    assert result.provider == "BackupProvider"
    assert len(llm.calls) == 2


def test_configured_actual_model_outside_adapter_allowlist_exhausts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_from(tmp_path, monkeypatch, configured_yaml())
    llm = RecordingOfficialLlm()
    mismatch = host_result(provider="PrimaryProvider", model="rogue-model")
    llm.queue("complete", mismatch)

    with pytest.raises(RoleLlmUnavailableError) as exc_info:
        RoleLlmRouter(llm, config).complete("writer", MESSAGES)

    assert exc_info.value.__cause__ is not None
    assert type(exc_info.value.__cause__).__name__ == "_ActualRouteMismatch"


def test_official_versioned_model_attribution_is_accepted_for_requested_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_from(tmp_path, monkeypatch, configured_yaml())
    llm = RecordingOfficialLlm()
    expected = host_result(
        provider="PrimaryProvider",
        model="PrimaryModel-2026-07-12",
    )
    llm.queue("complete", expected)

    assert RoleLlmRouter(llm, config).complete("writer", MESSAGES) is expected
    assert len(llm.calls) == 1


def test_non_version_model_suffix_and_different_namespace_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = load_from(tmp_path / "plain", monkeypatch, configured_yaml())
    plain_llm = RecordingOfficialLlm()
    plain_llm.queue(
        "complete",
        host_result(provider="PrimaryProvider", model="PrimaryModel-turbo"),
    )
    with pytest.raises(RoleLlmUnavailableError):
        RoleLlmRouter(plain_llm, plain).complete("writer", MESSAGES)

    namespaced = load_from(
        tmp_path / "namespaced",
        monkeypatch,
        """
routing:
  allowed_providers: [p]
  allowed_models: [ns-a/base]
roles:
  writer:
    route: configured
    provider: p
    model: ns-a/base
""",
    )
    namespaced_llm = RecordingOfficialLlm()
    namespaced_llm.queue(
        "complete",
        host_result(provider="p", model="ns-b/base-20260712"),
    )
    with pytest.raises(RoleLlmUnavailableError):
        RoleLlmRouter(namespaced_llm, namespaced).complete("writer", MESSAGES)


def test_host_cannot_silently_use_an_allowlisted_but_unconfigured_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_from(tmp_path, monkeypatch, configured_yaml())
    llm = RecordingOfficialLlm()
    llm.queue(
        "complete",
        host_result(provider="BackupProvider", model="BackupModel"),
    )

    with pytest.raises(RoleLlmUnavailableError) as exc_info:
        RoleLlmRouter(llm, config).complete("writer", MESSAGES)

    assert type(exc_info.value.__cause__).__name__ == "_ActualRouteMismatch"
    assert len(llm.calls) == 1


def test_inherit_accepts_whatever_active_route_host_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_from(tmp_path, monkeypatch)
    llm = RecordingOfficialLlm()
    expected = host_result(provider="any-active-provider", model="any-active-model")
    llm.queue("complete", expected)

    assert RoleLlmRouter(llm, config).complete("writer", MESSAGES) is expected


def test_result_identity_usage_and_host_audit_are_preserved_with_copied_adapter_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_from(tmp_path, monkeypatch)
    llm = RecordingOfficialLlm()
    result = host_result()
    usage = result.usage
    original_audit = result.audit
    llm.queue("complete", result)

    returned = RoleLlmRouter(llm, config).complete("reader_editor", MESSAGES)

    assert returned is result
    assert returned.usage is usage
    assert returned.audit is not original_audit
    assert original_audit == {"host": "preserved"}
    assert returned.audit["host"] == "preserved"
    assert returned.audit["role"] == "reader_editor"
    assert returned.audit["route"] == "inherit"


def test_audit_sink_failures_never_mask_success_or_unavailability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_from(tmp_path, monkeypatch)

    def broken_sink(event: dict[str, Any]) -> None:
        raise RuntimeError("sink down")

    success_llm = RecordingOfficialLlm()
    expected = host_result()
    success_llm.queue("complete", expected)
    assert RoleLlmRouter(success_llm, config, broken_sink).complete("writer", MESSAGES) is expected

    failed_llm = RecordingOfficialLlm()
    cause = TimeoutError("host down")
    failed_llm.queue("complete", cause)
    with pytest.raises(RoleLlmUnavailableError) as exc_info:
        RoleLlmRouter(failed_llm, config, broken_sink).complete("writer", MESSAGES)
    assert exc_info.value.__cause__ is cause


def test_sink_events_never_contain_prompt_or_structured_instructions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_from(tmp_path, monkeypatch)
    llm = RecordingOfficialLlm()
    llm.queue("complete_structured", host_result(structured=True))
    events: list[dict[str, Any]] = []

    RoleLlmRouter(llm, config, events.append).complete_structured(
        "context_curator",
        instructions="highly private instructions",
        input=[{"type": "text", "text": "secret source"}],
        json_schema={"type": "object"},
    )

    serialized = repr(events)
    assert "private instructions" not in serialized
    assert "secret source" not in serialized
    assert "messages" not in events[0]
    assert "instructions" not in events[0]
    assert "input" not in events[0]


def test_sync_structured_calls_exact_official_method_kwargs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_from(tmp_path, monkeypatch)
    llm = RecordingOfficialLlm()
    expected = host_result(structured=True)
    llm.queue("complete_structured", expected)
    schema = {"type": "object"}
    inputs = [{"type": "text", "text": "source"}]

    returned = RoleLlmRouter(llm, config).complete_structured(
        "context_curator",
        instructions="extract",
        input=inputs,
        json_schema=schema,
        json_mode=True,
        schema_name="facts",
        system_prompt="system",
    )

    assert returned is expected
    assert llm.calls == [
        (
            "complete_structured",
            {
                "instructions": "extract",
                "input": inputs,
                "json_schema": schema,
                "json_mode": True,
                "schema_name": "facts",
                "system_prompt": "system",
                "provider": None,
                "model": None,
                "temperature": 0.1,
                "max_tokens": None,
                "timeout": 120.0,
                "purpose": "webnovel:context_curator",
            },
        )
    ]


@pytest.mark.asyncio
async def test_async_complete_has_same_configured_fallback_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_from(
        tmp_path,
        monkeypatch,
        configured_yaml(
            fallbacks="    fallbacks:\n      - provider: BackupProvider\n        model: BackupModel\n"
        ),
    )
    llm = RecordingOfficialLlm()
    expected = host_result(provider="BackupProvider", model="BackupModel")
    llm.queue("acomplete", TimeoutError("down"), expected)

    returned = await RoleLlmRouter(llm, config).acomplete("writer", MESSAGES)

    assert returned is expected
    assert [name for name, _ in llm.calls] == ["acomplete", "acomplete"]
    assert returned.audit["attempt"] == 2
    assert returned.audit["fallback_used"] is True


@pytest.mark.asyncio
async def test_async_structured_validation_error_never_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_from(
        tmp_path,
        monkeypatch,
        configured_yaml(
            fallbacks="    fallbacks:\n      - provider: BackupProvider\n        model: BackupModel\n"
        ),
    )
    llm = RecordingOfficialLlm()
    error = ValueError("structured input is invalid")
    llm.queue(
        "acomplete_structured",
        error,
        host_result(provider="BackupProvider", model="BackupModel", structured=True),
    )

    with pytest.raises(RoleLlmInvocationError) as exc_info:
        await RoleLlmRouter(llm, config).acomplete_structured(
            "writer",
            instructions="extract",
            input=[{"type": "text", "text": "chapter"}],
        )

    assert exc_info.value.__cause__ is error
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_async_structured_calls_exact_official_method_and_returns_host_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_from(tmp_path, monkeypatch)
    llm = RecordingOfficialLlm()
    expected = host_result(structured=True)
    llm.queue("acomplete_structured", expected)

    returned = await RoleLlmRouter(llm, config).acomplete_structured(
        "reader_editor",
        instructions="check",
        input=[{"type": "text", "text": "chapter"}],
    )

    assert returned is expected
    method, kwargs = llm.calls[0]
    assert method == "acomplete_structured"
    assert kwargs == {
        "instructions": "check",
        "input": [{"type": "text", "text": "chapter"}],
        "json_schema": None,
        "json_mode": False,
        "schema_name": None,
        "system_prompt": None,
        "provider": None,
        "model": None,
        "temperature": 0.3,
        "max_tokens": None,
        "timeout": 180.0,
        "purpose": "webnovel:reader_editor",
    }
