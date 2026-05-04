from __future__ import annotations

import functools
import inspect
import types
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Union, get_args, get_origin, get_type_hints

from databricks.sdk.config import Config

"""Environment-aware factory for :class:`databricks.sdk.config.Config`.

Designed for services (MCP servers, brokers, multi-tenant backends, etc.)
that need to build a per-request ``Config`` from client-supplied env vars
forwarded as-is, rather than reading ``os.environ`` of the host process.
The SDK's own auto-discovery only consults the process env, which is the
wrong scope for a service serving many callers.

Wraps the SDK's ``Config`` with a small helper that accepts an explicit
``env`` mapping in addition to keyword arguments and coerces string values
into the types declared on the matching ``Config`` attribute
(``bool`` / ``int`` / ``float`` / ``str``).

Env keys are resolved in this order:

* explicit ``ConfigAttribute.env`` and ``env_aliases`` declared on the SDK,
* ``DATABRICKS_<NAME>`` -> ``<name>``,
* ``ARM_<NAME>`` -> ``azure_<name>``.

Keyword arguments to :func:`create` always win over ``env`` values.
"""

# ---------- Constants ----------

_DATABRICKS_PREFIX = "DATABRICKS_"
_ARM_PREFIX = "ARM_"
_TRUE_VALUES = {"1", "true", "yes", "y", "on"}
_FALSE_VALUES = {"0", "false", "no", "n", "off"}

# ---------- ConfigParam ----------


@dataclass(frozen=True)
class _ConfigParam:
    """Resolved metadata for a single :class:`Config` attribute.

    ``base_type`` is the unwrapped runtime type used to coerce env values
    (e.g. for ``int | None`` ``base_type`` is ``int`` and ``nullable`` is
    ``True``). ``env_names`` are the env var keys that should map directly
    to this field, including any aliases declared on the SDK descriptor.
    """

    name: str
    annotation: Any
    base_type: Any
    nullable: bool
    env_names: tuple[str, ...] = ()

    @staticmethod
    def from_annotation(
        name: str, ann: Any, env_names: tuple[str, ...] = ()
    ) -> "_ConfigParam":
        """Build a ``_ConfigParam`` from a type annotation.

        Strips a single ``Optional[...]`` / ``T | None`` layer to recover
        the coercion target. Anything ambiguous (``Union[int, str]``,
        missing annotation, ``Any``) falls back to ``base_type=str`` so
        values are passed through unchanged.
        """
        if ann in {inspect.Parameter.empty, Any}:
            return _ConfigParam(
                name=name,
                annotation=ann,
                base_type=str,
                nullable=True,
                env_names=env_names,
            )

        origin = get_origin(ann)
        args = get_args(ann)
        base_type = ann
        nullable = False
        if origin in {types.UnionType, Union}:
            non_none = [a for a in args if a is not type(None)]
            base_type = non_none[0] if len(non_none) == 1 else Any
            nullable = type(None) in args

        return _ConfigParam(
            name=name,
            annotation=ann,
            base_type=base_type,
            nullable=nullable,
            env_names=env_names,
        )


# ---------- Public API ----------


def create(*args, env: Mapping[str, str] | None = None, **kwargs) -> Config:
    """Build a :class:`Config` from kwargs plus an explicit ``env`` mapping.

    Each ``env`` key is mapped to a ``Config`` field via the SDK's declared
    env names (and aliases) or the ``DATABRICKS_`` / ``ARM_`` conventions.
    Values for known fields are coerced into their declared type; unknown
    keys are ignored. Keyword arguments take precedence over ``env``.
    """
    config_kwargs = kwargs.copy()

    if env:
        for key, value in env.items():
            field = _env_key_to_field(key)

            if not field or field in config_kwargs:
                continue

            meta = _config_params().get(field)

            if not meta:
                continue

            config_kwargs[field] = _coerce_value(value, meta)

    return Config(*args, **config_kwargs)


# ---------- Utils ----------


@functools.cache
def _config_attribute_type() -> type | None:
    """Return the SDK's ``ConfigAttribute`` class if importable, else ``None``."""
    try:
        from databricks.sdk.config import ConfigAttribute

        return ConfigAttribute
    except ImportError:
        return None


@functools.cache
def _config_params() -> dict[str, _ConfigParam]:
    """Discover all configurable :class:`Config` fields and their metadata.

    Walks the SDK's ``ConfigAttribute`` descriptors first (they carry env
    names and reliable type annotations) and falls back to introspecting
    :meth:`Config.__init__` for any remaining named keyword parameters.
    Variadic ``*args`` / ``**kwargs`` parameters are skipped so they don't
    masquerade as real config fields.
    """
    config_params: dict[str, _ConfigParam] = {}

    if config_attribute_type := _config_attribute_type():
        hints = get_type_hints(Config)

        for name, value in vars(Config).items():
            if isinstance(value, config_attribute_type):
                ann = hints.get(name, inspect.Parameter.empty)
                env_names = _attribute_env_names(value)
                config_params[name] = _ConfigParam.from_annotation(
                    name, ann, env_names=env_names
                )

    skip_kinds = {
        inspect.Parameter.VAR_POSITIONAL,
        inspect.Parameter.VAR_KEYWORD,
    }
    for name, param in inspect.signature(Config).parameters.items():
        if param.kind in skip_kinds or name in config_params:
            continue
        config_params[name] = _ConfigParam.from_annotation(name, param.annotation)

    return config_params


def _attribute_env_names(attribute: Any) -> tuple[str, ...]:
    """Collect the primary env name and any aliases declared on a descriptor."""
    names: list[str] = []
    primary = getattr(attribute, "env", None)
    if primary:
        names.append(primary)
    for alias in getattr(attribute, "env_aliases", None) or ():
        if alias and alias not in names:
            names.append(alias)
    return tuple(names)


@functools.cache
def _env_lookup() -> dict[str, str]:
    """Reverse map of every declared env name (and alias) to field name."""
    lookup: dict[str, str] = {}
    for name, meta in _config_params().items():
        for env_name in meta.env_names:
            lookup[env_name] = name
    return lookup


def _env_key_to_field(key: str) -> str | None:
    """Translate an env var key into a :class:`Config` field name.

    Returns ``None`` if the key is not recognized either as an explicit
    descriptor env name/alias or via the ``DATABRICKS_`` / ``ARM_``
    conventions.
    """
    if (field := _env_lookup().get(key)) is not None:
        return field

    if key.startswith(_DATABRICKS_PREFIX):
        return key.removeprefix(_DATABRICKS_PREFIX).lower()

    if key.startswith(_ARM_PREFIX):
        return "azure_" + key.removeprefix(_ARM_PREFIX).lower()

    return None


def _coerce_value(value: str, meta: _ConfigParam) -> Any:
    """Coerce a string env value into the type declared on ``meta``.

    Empty strings become ``None`` for nullable fields. Recognized scalar
    types (``bool`` / ``int`` / ``float`` / ``str``) are converted directly;
    anything else (or a parse failure) is passed through unchanged so the
    SDK can apply its own validation.
    """
    if value == "" and meta.nullable:
        return None
    t = meta.base_type
    try:
        if t is bool:
            bool_value = _parse_bool(value)
            if bool_value is not None:
                return bool_value
        elif t is int:
            return int(value)
        elif t is float:
            return float(value)
        elif t is str:
            return value
    except ValueError:
        pass

    return value


def _parse_bool(value: str) -> bool | None:
    """Parse a human-readable bool string, returning ``None`` if unrecognized."""
    v = value.strip().lower()

    if v in _TRUE_VALUES:
        return True

    if v in _FALSE_VALUES:
        return False

    return None
