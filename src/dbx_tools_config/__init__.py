from __future__ import annotations

import functools
import hashlib
import json
from collections.abc import Collection, Iterable, Mapping
from typing import Any, TypeGuard

from databricks.sdk.config import Config, ConfigAttribute

"""Build and fingerprint :class:`databricks.sdk.config.Config` instances
from server-supplied inputs.

The primary use case is a service - MCP server, broker, sidecar,
multi-tenant backend - that needs to materialise a per-request
:class:`Config` from inputs that arrive on the wire. A typical mapping:

* HTTP headers (or env-shaped frames forwarded by the client) -> ``env``.
* POST-body / RPC-payload Config fields -> ``**kwargs``.
* An optional pre-resolved :class:`Config` -> ``config``, used as a
  baseline when augmenting an existing identity.

Precedence is ``kwargs > env > config`` (last write wins) and every
layer is optional. ``env`` values may be a single ``str``, an
``Iterable[str]`` (first element wins, matching how multi-value
HTTP/RPC frames expose headers) or ``None``.

Two helpers cover the per-request lifecycle:

* :func:`create_config` builds a real :class:`Config`. This is the
  expensive path: ``Config.__init__`` resolves host metadata over HTTP
  (``/.well-known/databricks-config`` GET), reads ``~/.databrickscfg``
  from disk and runs ``init_auth`` (which can itself touch the network
  or filesystem depending on the credential strategy).
* :func:`config_params_hash` returns a stable SHA-256 fingerprint of
  the same resolved inputs, computed entirely in-memory. Use it to
  cache or rate-limit per-identity clients without paying
  ``Config.__init__``'s cost on every request.

Note: env values are passed to :class:`Config` as-is. The SDK's
descriptor ``transform`` (typically the annotated type, plus custom
transforms for ``cloud`` / ``scopes``) does any conversion. This module
does not do its own string-to-bool/int/float coercion.
"""


"""Type alias for the ``env`` argument to :func:`config_params`,
:func:`create_config` and :func:`config_params_hash`.

An env-shaped mapping where each value is either a single ``str``, an
``Iterable[str]`` (first element wins, matching multi-value HTTP /
multidict frames) or ``None``."""
ConfigEnv = Mapping[str, Iterable[str] | str | None] | Iterable[tuple[str, str | None]]

"""Fields stripped from :func:`config_params_hash` because they identify
*where* config came from (``profile`` / ``config_file`` /
``databricks_cli_path`` - lookup hints) or are *derived* during
``Config.__init__`` from other already-hashed fields (``auth_type`` is
written by ``init_auth`` from the credential strategy;
``databricks_environment`` is derived from ``host``). Two configs that
resolve to the same logical identity via different load paths or
credential strategies produce the same fingerprint."""
_HASH_IGNORE_FIELDS = [
    "profile",
    "config_file",
    "databricks_cli_path",
    "auth_type",
    "databricks_environment",
]


def config_params(
    config: Config | None = None,
    env: ConfigEnv | None = None,
    **kwargs,
) -> dict[str, Any]:
    """Merge ``config`` + ``env`` + ``kwargs`` into a single kwargs dict
    suitable for ``Config(**...)``.

    Precedence (last write wins):

    1. ``config.as_dict()`` if ``config`` is provided.
    2. ``env`` values, keyed by the SDK's declared
       :attr:`ConfigAttribute.env` name (or any
       :attr:`ConfigAttribute.env_aliases`). Each value may be:

       * a string - used directly,
       * ``None`` - sets the field to ``None`` (explicitly clearing
         anything inherited from ``config``),
       * an iterable of strings - the first element is used; an empty
         iterable leaves the field untouched.

    3. ``kwargs``.

    Unknown env keys are silently ignored.
    """
    merged: dict[str, Any] = {}

    if config:
        merged.update(config.as_dict())

    if env:
        if not isinstance(env, Mapping):
            env_map: Mapping = {}
            for item in env:
                env_key = item[0]
                if env_key not in env_map:
                    env_map[env_key] = []
                env_map[env_key].append(item[1])
        else:
            env_map: Mapping = env

        for env_key, attribute in _env_attributes().items():
            value = env_map.get(env_key, None)
            if value is None or isinstance(value, str):
                merged[attribute.name] = value
            else:
                for item in value:
                    merged[attribute.name] = item
                    break

    merged.update(kwargs)

    return merged


def config_params_hash(
    config: Config | None = None,
    env: ConfigEnv | None = None,
    **kwargs,
) -> str:
    """Return a stable SHA-256 hex digest of the resolved Config kwargs.

    Designed as a cache or rate-limit key for the same caller inputs
    that would be passed to :func:`create_config`. This is the cheap
    path: it operates purely on the merged dict and never constructs a
    :class:`Config`. Constructing a :class:`Config` triggers
    ``_resolve_host_metadata`` (HTTP GET to ``host``'s
    ``/.well-known/databricks-config``), ``_known_file_config_loader``
    (filesystem read of ``~/.databrickscfg``) and ``init_auth`` (which
    can itself touch the network or filesystem depending on the
    credential strategy). A service that wants to dedupe per-caller
    clients should fingerprint with :func:`config_params_hash` first
    and only call :func:`create_config` on cache miss.

    Fields in :data:`_HASH_IGNORE_FIELDS` are stripped before hashing
    (file lookup hints and derived auth metadata) so two callers that
    resolve to the same identity via different load paths fingerprint
    the same way.

    Values are normalised through ``str()`` and JSON-quoted as they're
    streamed into the digest. Mappings are emitted with their (encoded)
    keys sorted so dict ordering doesn't affect the digest. ``None``
    collapses with the empty string.
    """

    hasher = hashlib.sha256()

    def _str(value: Any, quote: bool = False) -> str:
        value_str = str(value) if value is not None else ""
        return json.dumps(value_str) if quote else value_str

    def _update(value: Any, quote: bool = False):
        hasher.update(_str(value, quote).encode("utf-8"))

    def _hash(value: Any):
        if isinstance(value, Mapping):
            _update("{")
            key_map = {_str(k, quote=True): k for k in value}
            for key_str in sorted(key_map.keys()):
                _update(key_str, quote=False)
                _update(":")
                _hash(value[key_map[key_str]])
                _update(",")
            _update("}")
        elif _is_collection(value):
            _update("[")
            for item in value:
                _hash(item)
                _update(",")
            _update("]")
        else:
            _update(value, quote=True)

    filtered: dict[str, Any] = {}
    for key, value in config_params(config, env, **kwargs).items():
        if key not in _HASH_IGNORE_FIELDS:
            filtered[key] = value

    _hash(filtered)
    return hasher.hexdigest()


def create_config(
    config: Config | None = None,
    env: ConfigEnv | None = None,
    **kwargs,
) -> Config:
    """Build a new :class:`Config` from ``config`` + ``env`` + ``kwargs``.

    Equivalent to ``Config(**config_params(config, env, **kwargs))``.
    See :func:`config_params` for the precedence rules and ``env``
    value semantics.
    """

    return Config(**config_params(config, env, **kwargs))


@functools.cache
def _env_attributes() -> dict[str, ConfigAttribute]:
    """Build a cached env-name -> :class:`ConfigAttribute` lookup.

    Includes both each attribute's primary :attr:`ConfigAttribute.env` name
    and any :attr:`ConfigAttribute.env_aliases`. Primary names are
    populated first so they take precedence over aliases on collision; the
    first writer wins for any subsequent collisions.
    """

    attributes = Config.attributes()
    if not isinstance(attributes, list):
        attributes = list(attributes)

    env_attributes: dict[str, ConfigAttribute] = {}

    def _set_env_attribute(attribute: ConfigAttribute, env: Any):
        if isinstance(env, str) and env and env not in env_attributes:
            env_attributes[env] = attribute

    for attribute in attributes:
        env = getattr(attribute, "env", None)
        _set_env_attribute(attribute, env)

    for attribute in attributes:
        env_aliases = getattr(attribute, "env_aliases", None)
        if _is_collection(env_aliases):
            for env_alias in env_aliases:
                _set_env_attribute(attribute, env_alias)

    return env_attributes


def _is_collection(value: Any) -> TypeGuard[Collection[Any]]:
    """Return True if ``value`` is an iterable container we want to walk
    element-by-element.

    Excludes ``str`` / ``bytes`` / ``bytearray`` since they're technically
    ``Collection`` instances but should be treated as scalars by the
    callers in this module (env-value coercion and hash recursion).

    Declared as a :class:`TypeGuard` so type checkers narrow ``value`` to
    ``Collection[Any]`` after the call, removing spurious "not iterable"
    warnings when the source value is typed as ``Any | None``.
    """
    return isinstance(value, Collection) and not isinstance(value, str | bytes | bytearray)


if __name__ == "__main__":
    config = create_config(env={"DATABRICKS_CONFIG_PROFILE": ["RACETRAC-DEV"]})
    print(config.as_dict())
    config = create_config(env={"DATABRICKS_CONFIG_PROFILE": ["DEFAULT"]})
    print(config.as_dict())
    print(config_params_hash(config))
    config = create_config(
        env={
            "DATABRICKS_CONFIG_PROFILE": ["DEFAULT"],
            "DATABRICKS_CONFIG_FILE": "~/.databrickscfg",
        }
    )
    print(config.as_dict())
    print(config_params_hash(config))
    config = Config()
    print(config.as_dict())
    print(config_params_hash(config))
