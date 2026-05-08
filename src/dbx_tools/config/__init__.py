"""Build and fingerprint :class:`~databricks.sdk.config.Config` from caller-supplied inputs.

Services (MCP servers, brokers, multi-tenant backends, sidecars) usually must
not rely on process-wide ``os.environ``. Each request can supply:

* ``env``: HTTP headers or another env-shaped mapping / iterable of pairs.
* ``kwargs``: fields accepted by :class:`~databricks.sdk.config.Config`
  (typically from a JSON body or RPC).
* ``config``: an optional baseline :class:`~databricks.sdk.config.Config`
  (for example an identity already resolved upstream).

Public API: instantiate :class:`ConfigParams` with any subset of those
arguments. The result is a :class:`~collections.abc.Mapping` of merged kwargs
(only attributes that received a value appear). Per SDK attribute name,
precedence is **kwargs, then env, then ``config.as_dict()``** (first layer that
sets the field wins).

Use :meth:`ConfigParams.hash` when you only need a stable cache key (SHA-256,
in memory, no disk or network). Use :meth:`ConfigParams.create_config` when you
need a real ``Config``; that constructs ``Config(**merged)`` and therefore runs
the SDK init path (host metadata HTTP probe, ``~/.databrickscfg``, auth
bootstrap).

Env frame values are normalised to scalars for ``Config`` inside
:func:`_env_params` (first element of non-string iterables, empty iterables
skipped). Aside from that, values are forwarded as-is; the SDK attribute
descriptors perform type conversion. This module does not implement extra
string-to-bool coercion.

Public symbols are :class:`ConfigParams` and the type alias :obj:`ConfigEnv`.

:meth:`ConfigParams.hash` fingerprints every declared SDK config attribute;
attributes absent from the merged mapping are treated as ``None`` in the
digest.
"""

from __future__ import annotations

import functools
import hashlib
import json
from collections.abc import Collection, Iterable, Iterator, Mapping
from typing import Any, TypeGuard, cast

from databricks.sdk.config import Config, ConfigAttribute

ConfigEnv = Mapping[str, Iterable[str] | str | None] | Iterable[tuple[str, str | None]]
"""Structural type for the ``env`` argument to :class:`ConfigParams`.

Either:

* A **mapping** from env variable name (``ConfigAttribute.env`` or an
  ``env_aliases`` name) to a value, or
* An **iterable of** ``(name, value)`` pairs. If the same name appears more
  than once, the **first** occurrence wins.

Each value may be ``None``, a ``str``, or a non-string **iterable** of
candidates (first item wins). Iterables include one-shot iterators such as
``iter(["https://a", "https://b"])``. An **empty** iterable means this env
layer does not set that attribute (distinct from ``None``, which clears the
field for this layer). Byte-like values are treated as scalars, not iterables.
"""


_UNSET = object()


class ConfigParams(Mapping[str, Any]):
    """Read-only mapping of merged keyword arguments for ``Config(**...)``.

    Keys are :class:`ConfigAttribute` names declared on
    :class:`~databricks.sdk.config.Config`. The mapping is sparse: attributes
    never set by any layer are omitted (no default-filled dict).
    """

    def __init__(self, config: Config | None = None, env: ConfigEnv | None = None, **kwargs: Any):
        """Merge ``kwargs``, ``env``, and optional baseline ``config``.

        For each declared config attribute, the first non-missing value wins,
        scanning layers in order:

        1. ``kwargs`` (highest precedence).
        2. ``env`` after :func:`_env_params` (recognised env names and aliases
           only; see :obj:`ConfigEnv`).
        3. ``config.as_dict()`` when ``config`` is not ``None``.

        Unrecognised keys in ``env`` are ignored. Providing only part of an env
        frame does not clear other attributes that come solely from
        ``config``.
        """

        attr_sources: list[Mapping[str, Any] | None] = [
            kwargs,
            _env_params(env),
            config.as_dict() if config else None,
        ]
        data: dict[str, Any] = {}
        for attr in _attributes():
            attr_name = attr.name
            attr_value = _UNSET
            for param_source in attr_sources:
                if param_source:
                    value = param_source.get(attr_name, _UNSET)
                    if value is not _UNSET:
                        attr_value = value
                        break
            if attr_value is not _UNSET:
                data[attr_name] = attr_value
        self._data = data

    def __getitem__(self, key: str) -> Any:
        """Return the merged value for ``key`` (``ConfigAttribute`` name)."""
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        """Yield keys present in the merged mapping."""
        return iter(self._data)

    def __len__(self) -> int:
        """Return the number of keys in the merged mapping."""
        return len(self._data)

    def __repr__(self) -> str:
        """Return a constructor-like repr of the internal merged ``dict``."""
        return f"{type(self).__name__}({self._data!r})"

    def hash(self) -> str:
        """Return a stable SHA-256 hex digest (64 lowercase hex chars).

        Fingerprint the same logical inputs as :meth:`create_config` without
        constructing a :class:`~databricks.sdk.config.Config`. Suitable for
        client caches and rate limits.

        The digest includes every declared SDK config attribute; attributes not
        present in the merged mapping contribute ``None`` for that key.

        Normalisation: values are canonicalised via :func:`_hash_data`
        (``str()`` for scalars; sorted keys for mappings). ``None`` and ``""``
        hash identically for a given key.

        """
        data = {attr.name: self._data.get(attr.name, None) for attr in _attributes()}
        serialized = _hash_dumps(_hash_data(data))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def create_config(self) -> Config:
        """Build ``Config(**self)``.

        Runs full SDK initialisation (network / filesystem side effects
        depending on credentials).
        """
        return Config(**self._data)


@functools.cache
def _attributes() -> list[ConfigAttribute]:
    """Return the SDK-declared :class:`ConfigAttribute` list for ``Config`` (cached)."""
    return list(Config.attributes())


@functools.cache
def _env_attributes() -> dict[str, ConfigAttribute]:
    """Cached lookup from env variable name to :class:`ConfigAttribute`.

    Includes each attribute's primary :attr:`ConfigAttribute.env` name and any
    :attr:`ConfigAttribute.env_aliases`. Primary names are registered first so
    they win when the same env string could map to multiple attributes.
    """

    env_attributes: dict[str, ConfigAttribute] = {}

    def _set_env_attribute(attribute: ConfigAttribute, env: Any) -> None:
        """Register ``env`` as mapping to ``attribute`` if it is a non-empty string and free."""

        if isinstance(env, str) and env and env not in env_attributes:
            env_attributes[env] = attribute

    for attribute in _attributes():
        env = getattr(attribute, "env", None)
        _set_env_attribute(attribute, env)

    for attribute in _attributes():
        env_aliases = getattr(attribute, "env_aliases", None)
        if _is_collection(env_aliases):
            for env_alias in env_aliases:
                _set_env_attribute(attribute, env_alias)

    return env_attributes


def _env_params(env: ConfigEnv | None) -> Mapping[str, Any] | None:
    """Map recognised env keys from ``env`` to :class:`Config` attribute names.

    Returns ``None`` when ``env`` is empty or falsy. Non-mapping ``env`` is
    folded into a mapping with **first** occurrence of each key retained.
    Unrecognised keys are skipped. Values are normalised to scalars: ``None``
    clears the attribute for this layer; mappings and byte-like strings pass
    through; non-empty :func:`_is_collection` values use their first element;
    empty collections omit the attribute; other :class:`~collections.abc.Iterable`
    values use ``next(iter(...))`` or omit on exhaustion.
    """
    if not env:
        return None
    if not isinstance(env, Mapping):
        env_map: Mapping[str, Any] = {}
        for item in env:
            key = item[0]
            if key not in env_map:
                env_map[key] = item[1]
    else:
        env_map = cast(Mapping[str, Any], env)

    env_params: dict[str, Any] = {}
    for env_key, attribute in _env_attributes().items():
        if attribute.name in env_params:
            continue
        env_value = env_map.get(env_key, _UNSET)
        if env_value is _UNSET:
            continue
        elif _is_collection(env_value):
            if not env_value:
                continue
            else:
                for v in env_value:
                    env_value = v
                    break
        env_params[attribute.name] = env_value
    return env_params


def _hash_data(value: Any) -> Any:
    """Recursively normalise ``value`` for stable JSON hashing.

    ``None`` becomes ``""``. Mappings become sorted-key dicts of normalised
    entries. Non-byte-like collections become lists of normalised elements (sets
    sorted). Other values become ``str(value)``.
    """
    if value is None:
        return ""
    if isinstance(value, Mapping):
        return {
            _hash_data(k): _hash_data(v)
            for k, v in sorted(
                value.items(),
                key=lambda item: _hash_sort_key(item[0]),
            )
        }
    elif _is_collection(value):
        if isinstance(value, set):
            value = sorted(value, key=_hash_sort_key)
        return [_hash_data(v) for v in value]
    else:
        return str(value)


def _hash_sort_key(value: Any) -> str:
    """Return a canonical string key for ordering hashable structures."""
    return _hash_dumps(_hash_data(value))


def _hash_dumps(value: Any) -> str:
    """JSON-serialise ``value`` with sorted keys and minimal separators."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _is_collection(value: Any) -> TypeGuard[Iterable[Any]]:
    """Return True if ``value`` is a collection to traverse element-wise.

    Excludes ``str``, ``bytes``, and ``bytearray`` so they stay scalar for env
    coercion and hashing.

    Declared as :class:`TypeGuard` so static analysis narrows ``value`` after a
    True result.
    """
    return isinstance(value, Iterable) and not isinstance(value, Mapping | str | bytes | bytearray)
