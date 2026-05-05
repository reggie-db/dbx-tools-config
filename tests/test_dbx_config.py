from __future__ import annotations

from typing import Any

import pytest

import dbx_config
from dbx_config import (
    _HASH_IGNORE_FIELDS,
    _env_attributes,
    create,
    param_hash,
    params,
)

"""Tests for :mod:`dbx_config`.

Covers the three public helpers (``params``, ``create``, ``param_hash``),
the cached env-name lookup (``_env_attributes``) and the
``_HASH_IGNORE_FIELDS`` constant. ``Config`` construction is stubbed so
nothing here hits the network.
"""


# ---------- _env_attributes ----------


class TestEnvAttributes:
    def test_returns_non_empty_mapping(self):
        dbx_config.create()
        attrs = _env_attributes()
        assert isinstance(attrs, dict)
        assert attrs

    @pytest.mark.parametrize(
        "env_key,field",
        [
            ("DATABRICKS_HOST", "host"),
            ("DATABRICKS_TOKEN", "token"),
            ("DATABRICKS_CLUSTER_ID", "cluster_id"),
            ("DATABRICKS_WAREHOUSE_ID", "warehouse_id"),
            ("DATABRICKS_CONFIG_PROFILE", "profile"),
            ("DATABRICKS_CONFIG_FILE", "config_file"),
            ("DATABRICKS_AZURE_RESOURCE_ID", "azure_workspace_resource_id"),
            ("ARM_TENANT_ID", "azure_tenant_id"),
            ("ARM_USE_MSI", "azure_use_msi"),
            ("GOOGLE_CREDENTIALS", "google_credentials"),
        ],
    )
    def test_known_env_names(self, env_key, field):
        assert _env_attributes()[env_key].name == field

    def test_oidc_alias_resolves_to_filepath(self):
        # ``DATABRICKS_OIDC_TOKEN_FILE`` is registered via ``env_aliases``.
        assert _env_attributes()["DATABRICKS_OIDC_TOKEN_FILE"].name == "oidc_token_filepath"

    def test_primary_and_alias_share_attribute(self):
        attrs = _env_attributes()
        assert attrs["DATABRICKS_OIDC_TOKEN_FILEPATH"] is attrs["DATABRICKS_OIDC_TOKEN_FILE"]

    def test_unknown_env_key_absent(self):
        assert "NOT_A_DATABRICKS_ENV_VAR" not in _env_attributes()


# ---------- _HASH_IGNORE_FIELDS ----------


class TestHashIgnoreFields:
    def test_includes_source_lookup_fields(self):
        for field in ("profile", "config_file", "databricks_cli_path"):
            assert field in _HASH_IGNORE_FIELDS

    def test_includes_derived_fields(self):
        for field in ("auth_type", "databricks_environment"):
            assert field in _HASH_IGNORE_FIELDS

    @pytest.mark.parametrize(
        "field",
        [
            "host",
            "token",
            "account_id",
            "workspace_id",
            "cluster_id",
            "warehouse_id",
            "client_id",
            "client_secret",
            "azure_tenant_id",
            "azure_workspace_resource_id",
            "google_credentials",
        ],
    )
    def test_does_not_ignore_identity_fields(self, field):
        # Identity-bearing fields must NOT be in the ignore list, otherwise
        # ``param_hash`` would collapse distinct identities.
        assert field not in _HASH_IGNORE_FIELDS


# ---------- params() ----------


class _ConfigStub:
    """Stand-in with a Config-shaped ``as_dict()`` for params()/param_hash()."""

    def __init__(self, **fields: Any) -> None:
        self._fields = fields

    def as_dict(self) -> dict[str, Any]:
        return dict(self._fields)


class TestParams:
    def test_no_inputs_returns_empty(self):
        assert params() == {}

    def test_kwargs_only(self):
        assert params(host="https://x", token="y") == {
            "host": "https://x",
            "token": "y",
        }

    def test_config_baseline_forwarded(self):
        cfg = _ConfigStub(host="https://x", token="y")
        assert params(config=cfg) == {"host": "https://x", "token": "y"}  # pyright: ignore[reportArgumentType]

    def test_env_str_value(self):
        out = params(env={"DATABRICKS_HOST": "https://env"})
        assert out["host"] == "https://env"

    def test_env_iterable_first_wins(self):
        out = params(env={"DATABRICKS_HOST": ["https://a", "https://b"]})
        assert out["host"] == "https://a"

    def test_env_iterator_first_wins(self):
        out = params(env={"DATABRICKS_HOST": iter(["https://a", "https://b"])})
        assert out["host"] == "https://a"

    def test_env_empty_iterable_leaves_field_unset(self):
        # The inner ``for value in value`` never executes, so the
        # attribute name is never assigned in this branch.
        out = params(env={"DATABRICKS_HOST": []})
        assert "host" not in out

    def test_env_explicit_none_sets_none(self):
        out = params(env={"DATABRICKS_HOST": None})
        assert out["host"] is None

    def test_env_alias_resolves(self):
        out = params(env={"DATABRICKS_OIDC_TOKEN_FILE": "/path"})
        assert out["oidc_token_filepath"] == "/path"

    def test_env_alias_overrides_primary_when_both_provided(self):
        # Iteration order is primary first, alias second; the alias write
        # is therefore last-write-wins for the shared attribute name.
        out = params(
            env={
                "DATABRICKS_OIDC_TOKEN_FILEPATH": "/primary",
                "DATABRICKS_OIDC_TOKEN_FILE": "/alias",
            }
        )
        assert out["oidc_token_filepath"] == "/alias"

    def test_env_unknown_key_ignored(self):
        out = params(env={"NOT_A_DATABRICKS_ENV_VAR": "x"})
        assert "NOT_A_DATABRICKS_ENV_VAR" not in out
        # The unknown value must not leak under any field name.
        assert "x" not in out.values()

    def test_kwargs_win_over_env(self):
        out = params(host="https://kwargs", env={"DATABRICKS_HOST": "https://env"})
        assert out["host"] == "https://kwargs"

    def test_env_wins_over_config(self):
        cfg = _ConfigStub(host="https://config")
        out = params(config=cfg, env={"DATABRICKS_HOST": "https://env"})  # pyright: ignore[reportArgumentType]
        assert out["host"] == "https://env"

    def test_kwargs_win_over_config(self):
        cfg = _ConfigStub(host="https://config")
        out = params(config=cfg, host="https://kwargs")  # pyright: ignore[reportArgumentType]
        assert out["host"] == "https://kwargs"

    def test_empty_env_does_not_iterate(self):
        # An empty mapping is falsy so the env loop is skipped entirely,
        # leaving the config baseline intact.
        cfg = _ConfigStub(host="https://config")
        out = params(config=cfg, env={})  # pyright: ignore[reportArgumentType]
        assert out == {"host": "https://config"}

    def test_env_iteration_writes_none_for_missing_keys(self):
        # Documented (current) behavior: providing *any* env triggers
        # iteration over every known env name, so missing keys end up as
        # ``None`` in the result.
        out = params(env={"DATABRICKS_TOKEN": "y"})
        assert out["token"] == "y"
        assert out["host"] is None
        assert out["cluster_id"] is None

    def test_env_wipes_config_baseline_for_missing_keys(self):
        # Current behavior: env iteration writes ``None`` for keys missing
        # from ``env``, including ones set on ``config``. ``kwargs`` is
        # the only way to preserve a baseline value when ``env`` is also
        # passed.
        cfg = _ConfigStub(host="https://config", token="t")
        out = params(config=cfg, env={"DATABRICKS_CLUSTER_ID": "c-1"})  # pyright: ignore[reportArgumentType]
        assert out["cluster_id"] == "c-1"
        assert out["host"] is None
        assert out["token"] is None


# ---------- create() ----------


class _RecordingConfig:
    """Replacement for ``databricks.sdk.config.Config`` that records init."""

    last_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_kwargs = kwargs


@pytest.fixture
def stub_config(monkeypatch):
    """Replace ``dbx_config.Config`` with a recording stub for ``create()``.

    Pre-warms ``_env_attributes`` first so the @functools.cache populates
    against the real SDK class before the patch hides it.
    """

    _env_attributes()
    monkeypatch.setattr(dbx_config, "Config", _RecordingConfig)
    return _RecordingConfig


class TestCreate:
    def test_returns_config_instance(self, stub_config):
        assert isinstance(create(host="https://x"), stub_config)

    def test_no_inputs_calls_config_with_no_kwargs(self, stub_config):
        create()
        assert stub_config.last_kwargs == {}

    def test_kwargs_only(self, stub_config):
        create(host="https://x")
        assert stub_config.last_kwargs == {"host": "https://x"}

    def test_env_only(self, stub_config):
        create(env={"DATABRICKS_HOST": "https://env"})
        assert stub_config.last_kwargs["host"] == "https://env"

    def test_kwargs_win_over_env(self, stub_config):
        create(host="https://kwargs", env={"DATABRICKS_HOST": "https://env"})
        assert stub_config.last_kwargs["host"] == "https://kwargs"

    def test_alias_resolves(self, stub_config):
        create(env={"DATABRICKS_OIDC_TOKEN_FILE": "/path"})
        assert stub_config.last_kwargs["oidc_token_filepath"] == "/path"


# ---------- param_hash() ----------


class TestParamHash:
    def test_returns_sha256_hex(self):
        digest = param_hash(host="https://x")
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_deterministic(self):
        a = param_hash(host="https://x", token="t")
        b = param_hash(host="https://x", token="t")
        assert a == b

    def test_no_inputs_is_stable_across_calls(self):
        # Don't pin the exact byte format (the streaming layout has
        # changed several times); just assert two no-input calls match.
        assert param_hash() == param_hash()

    def test_different_host_changes_hash(self):
        assert param_hash(host="https://a") != param_hash(host="https://b")

    @pytest.mark.parametrize(
        "field,extra_value",
        [
            ("token", "dapi-x"),
            ("cluster_id", "c-1"),
            ("warehouse_id", "w-1"),
            ("account_id", "acct-1"),
            ("azure_tenant_id", "tenant-1"),
        ],
    )
    def test_identity_fields_change_hash(self, field, extra_value):
        base = param_hash(host="https://x")
        with_extra = param_hash(host="https://x", **{field: extra_value})
        assert with_extra != base

    @pytest.mark.parametrize("ignored", _HASH_IGNORE_FIELDS)
    def test_ignored_fields_do_not_change_hash(self, ignored):
        base = param_hash(host="https://x")
        with_ignored = param_hash(host="https://x", **{ignored: "anything"})  # pyright: ignore[reportArgumentType]
        assert with_ignored == base

    def test_none_and_empty_string_hash_the_same(self):
        # ``None`` collapses to ``""`` before encoding so an explicit
        # ``None`` value hashes the same as an explicit empty string.
        assert param_hash(host=None) == param_hash(host="")

    def test_iterable_value_uses_first_element(self):
        # Going through ``env``, the iterable resolves to its first
        # element before hashing; everything else is set to ``None`` by
        # the env iteration so both calls produce identical inputs.
        a = param_hash(env={"DATABRICKS_HOST": ["https://a", "https://b"]})
        b = param_hash(env={"DATABRICKS_HOST": "https://a"})
        assert a == b

    def test_kwarg_order_does_not_change_hash(self):
        # The merged params are sorted internally before hashing so
        # caller kwarg order must not leak into the digest.
        a = param_hash(host="https://x", token="t", cluster_id="c-1")
        b = param_hash(cluster_id="c-1", token="t", host="https://x")
        assert a == b

    def test_config_baseline_equivalent_to_kwargs(self):
        # Hashing a config baseline must produce the same digest as
        # passing the same fields directly via kwargs.
        cfg = _ConfigStub(host="https://x", token="t")
        assert param_hash(config=cfg) == param_hash(host="https://x", token="t")  # pyright: ignore[reportArgumentType]


# ---------- ConfigEnv shape equivalence ----------


def _items() -> list[tuple[str, str | None]]:
    """Canonical (key, value) pairs used by every ConfigEnv-form fixture."""
    return [("DATABRICKS_HOST", "https://x"), ("DATABRICKS_TOKEN", "t")]


# Each entry is ``(id, factory)``. The factory is called per-assertion
# because some forms (generators, iterators) are one-shot and can't be
# reused between the params() and param_hash() calls.
_CONFIG_ENV_FORMS: list[tuple[str, Any]] = [
    ("mapping_str_value", lambda: dict(_items())),
    ("mapping_list_value", lambda: {k: [v] for k, v in _items()}),
    ("mapping_tuple_value", lambda: {k: (v,) for k, v in _items()}),
    ("mapping_iterator_value", lambda: {k: iter([v]) for k, v in _items()}),
    ("list_of_tuples", lambda: list(_items())),
    ("tuple_of_tuples", lambda: tuple(_items())),
    ("dict_items_view", lambda: dict(_items()).items()),
    ("generator_of_tuples", lambda: (pair for pair in _items())),
]

_FORM_IDS = [pair[0] for pair in _CONFIG_ENV_FORMS]
_FORM_FACTORIES = [pair[1] for pair in _CONFIG_ENV_FORMS]


def _baseline_params() -> dict[str, Any]:
    return params(env=dict(_items()))


def _baseline_hash() -> str:
    return param_hash(env=dict(_items()))


class TestConfigEnvForms:
    """Every shape allowed by :data:`dbx_config.ConfigEnv` must resolve
    to the same ``params()`` dict and the same ``param_hash()`` digest."""

    @pytest.mark.parametrize("env_factory", _FORM_FACTORIES, ids=_FORM_IDS)
    def test_form_resolves_to_same_params(self, env_factory):
        assert params(env=env_factory()) == _baseline_params()

    @pytest.mark.parametrize("env_factory", _FORM_FACTORIES, ids=_FORM_IDS)
    def test_form_produces_same_param_hash(self, env_factory):
        assert param_hash(env=env_factory()) == _baseline_hash()

    def test_iterable_of_tuples_first_value_wins_for_duplicate_keys(self):
        # Mirrors the Mapping form's first-wins-on-iterable behaviour.
        env = [("DATABRICKS_HOST", "https://a"), ("DATABRICKS_HOST", "https://b")]
        assert params(env=env)["host"] == "https://a"

    def test_iterable_of_tuples_matches_mapping_with_list_value(self):
        # ``[("X", "a"), ("X", "b")]`` must resolve identically to
        # ``{"X": ["a", "b"]}`` since the iterable form is collected
        # into a per-key list internally.
        as_tuples = [
            ("DATABRICKS_HOST", "https://a"),
            ("DATABRICKS_HOST", "https://b"),
        ]
        as_mapping = {"DATABRICKS_HOST": ["https://a", "https://b"]}
        assert params(env=as_tuples) == params(env=as_mapping)
        assert param_hash(env=as_tuples) == param_hash(env=as_mapping)

    def test_iterable_of_tuples_with_none_value_matches_mapping_none(self):
        # ``[("X", None)]`` must resolve identically to ``{"X": None}``.
        as_tuples = [("DATABRICKS_HOST", None)]
        as_mapping = {"DATABRICKS_HOST": None}
        assert params(env=as_tuples) == params(env=as_mapping)
        assert param_hash(env=as_tuples) == param_hash(env=as_mapping)
