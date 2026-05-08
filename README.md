# dbx-tools-config

Small helpers around `databricks.sdk.config.Config` for **services that build a `Config` per request** from caller-supplied inputs: MCP servers, brokers, multi-tenant backends, sidecars, agent frameworks, and similar.

The Databricks SDK discovers config from `os.environ` in the host process. That scope is wrong when one worker serves many tenants or sessions. This package lets each request supply env-shaped data plus optional kwargs and an optional baseline `Config`, then either fingerprint the inputs cheaply or construct a real `Config`.

Typical mapping for an HTTP or MCP-style request:

| Source                                  | Constructor argument |
| --------------------------------------- | ---------------------- |
| Request headers (env-shaped names)      | `env=`                 |
| POST body / RPC payload (`Config` keys) | `**kwargs`             |
| Pre-resolved `Config` baseline          | `config=`              |

Precedence **per SDK attribute** is **`kwargs` > `env` > `config.as_dict()`**: the first layer that supplies a value for that attribute wins. Layers are optional. The merged result only contains attributes that were set; it is not a full default-filled dict.

## API

Import from the namespace package **`dbx_tools.config`** (wheel module name from `pyproject`):

```python
from dbx_tools.config import ConfigParams, ConfigEnv
```

### `ConfigParams`

```python
class ConfigParams(Mapping[str, Any]):
    def __init__(
        self,
        config: Config | None = None,
        env: ConfigEnv | None = None,
        **kwargs: Any,
    ) -> None: ...

    def hash(self) -> str:
        """SHA-256 hex digest of merged params; no Config built."""

    def create_config(self) -> Config:
        """Config(**merged); runs full SDK __init__."""

```

- **`ConfigParams(...)`** returns a read-only mapping of merged kwargs suitable for `Config(**...)`.
- **`hash()`** returns a stable **SHA-256** hex digest from that merge **without** constructing `Config`. Use this for caches and rate limits.
- **`create_config()`** is **`Config(**merged)`**. **Expensive**: triggers host-metadata HTTP (`/.well-known/databricks-config`), `~/.databrickscfg` reads, and credential bootstrap as in upstream `Config.__init__`.

Equivalent patterns:

```python
params = ConfigParams(env=headers, **body)
client_config = params.create_config()

# Merged dict only
merged = dict(ConfigParams(env=headers, **body))
```

### `ConfigEnv`

Type alias (structural): either a **string-keyed mapping** of env var name to value, or an **iterable of `(name, value)`** pairs. Unrecognised names are ignored. Duplicate keys in the pair form: **first wins**.

## Install

Published on [PyPI](https://pypi.org/project/dbx-tools-config/):

```bash
pip install dbx-tools-config
```

In `pyproject.toml`:

```toml
[project]
dependencies = [
    "dbx-tools-config",
]
```

### Alternative: install directly from GitHub

Useful for pinning to an unreleased commit or pulling from a fork.
Works with `pip`, `uv`, `poetry`, etc. via the PEP 508 direct URL form.

`pyproject.toml`:

```toml
[project]
dependencies = [
    "dbx-tools-config @ git+https://github.com/reggie-db/dbx-tools-config",
]
```

Pin to a tag, branch or commit with the standard `@<ref>` suffix:

```toml
[project]
dependencies = [
    # tag
    "dbx-tools-config @ git+https://github.com/reggie-db/dbx-tools-config@v0.1.4",
    # branch
    "dbx-tools-config @ git+https://github.com/reggie-db/dbx-tools-config@main",
    # commit SHA
    "dbx-tools-config @ git+https://github.com/reggie-db/dbx-tools-config@<sha>",
]
```

Or install ad-hoc without editing `pyproject.toml`:

```bash
pip install 'git+https://github.com/reggie-db/dbx-tools-config'
uv add 'dbx-tools-config @ git+https://github.com/reggie-db/dbx-tools-config'
```

## Usage

### Server-style: per-request `Config` from headers + body

```python
from dbx_tools.config import ConfigParams
from databricks.sdk import WorkspaceClient

def handle_request(request):
    params = ConfigParams(
        env=request.headers,
        **request.json(),
    )
    config = params.create_config()
    return WorkspaceClient(config=config).do_work(...)
```

### Other shapes

```python
from dbx_tools.config import ConfigParams

# From an env-shaped mapping
params = ConfigParams(env={
    "DATABRICKS_HOST": "https://myworkspace.cloud.databricks.com",
    "DATABRICKS_TOKEN": "dapi...",
})

# From the process environment (single-tenant CLIs, scripts, tests)
import os
params = ConfigParams(env=os.environ)

# Kwargs override env for the same attribute
params = ConfigParams(
    host="https://override.cloud.databricks.com",
    env=client_env,
)

# Baseline Config plus overrides
params = ConfigParams(config=other_config, host="https://override...")

# Dict of merged kwargs only (no Config constructed)
merged = dict(ConfigParams(config=other_config, env=client_env))
```

## Env value semantics

Each value associated with a recognised env key may be:

| Value           | Behavior |
| --------------- | -------- |
| `str`           | Used directly. |
| `None`          | Sets the field to `None` for this layer (clears a baseline from `config=` for that attribute). |
| Iterable (not `str` / bytes) | First element is used (multi-value HTTP / multidict style). Includes iterators such as `iter([a, b])`. |
| Empty iterable  | That attribute is **not** set by this `env` layer (different from `None`). |

Partial `env` frames do **not** zero out other attributes that come only from `config`.

## Env key resolution

Each key in `env` is matched against the SDK's declared `ConfigAttribute.env` (and `env_aliases`) on `Config`. Examples the SDK declares today include:

- `DATABRICKS_HOST` → `host`
- `DATABRICKS_TOKEN` → `token`
- `DATABRICKS_CLUSTER_ID` → `cluster_id`
- `DATABRICKS_OIDC_TOKEN_FILE` → `oidc_token_filepath` (alias)
- `DATABRICKS_AZURE_RESOURCE_ID` → `azure_workspace_resource_id`
- `ARM_TENANT_ID` → `azure_tenant_id`
- `GOOGLE_CREDENTIALS` → `google_credentials`

Keys that do not match a declared env name or alias are ignored.

> This package does **not** add string-to-bool/int coercion beyond normalising env frames to scalars. Values are passed through to `Config(**kwargs)` and the SDK descriptors apply conversion. The SDK uses `bool(value)` for boolean fields, so the string `"false"` can become `True`. Pass real Python booleans via `kwargs` when that matters.

### Out of scope: ambient env vars

Some `databricks-sdk` features read env vars directly from `os.environ` instead of through `Config`:

- `DATABRICKS_RUNTIME_VERSION` (DBR detection / user-agent)
- `IS_IN_DB_MODEL_SERVING_ENV`, `IS_IN_DATABRICKS_MODEL_SERVING_ENV`,
  `DATABRICKS_MODEL_SERVING_HOST_URL`, `DB_MODEL_SERVING_HOST_URL`
  (model serving auto-auth)
- `ACTIONS_ID_TOKEN_REQUEST_TOKEN`, `ACTIONS_ID_TOKEN_REQUEST_URL`
  (GitHub Actions OIDC)
- `SYSTEM_ACCESSTOKEN`, `SYSTEM_*` (Azure DevOps OIDC)
- `AGENT` (user-agent)

Passing these through `ConfigParams(env=...)` does not affect code paths that bypass `Config`. Set them on `os.environ` in the worker if you rely on them.

## Hashing

`ConfigParams(...).hash()` returns a stable SHA-256 hex digest without constructing `Config`. `Config.__init__` is costly because it runs steps such as:

1. `_resolve_host_metadata` — HTTP `GET host/.well-known/databricks-config`
2. `_known_file_config_loader` — reads `~/.databrickscfg` when auth is indirect
3. `_validate` — conflicting auth checks
4. `init_auth` — credential strategy (CLI, token files, etc.)

Example cache keyed by fingerprint:

```python
from dbx_tools.config import ConfigParams
from databricks.sdk import WorkspaceClient

_clients: dict[str, WorkspaceClient] = {}

def client_for(request):
    params = ConfigParams(env=request.headers, **request.json())
    key = params.hash()
    client = _clients.get(key)
    if client is None:
        client = _clients[key] = WorkspaceClient(config=params.create_config())
    return client
```

The digest includes **every** declared `Config` attribute from the SDK; attributes you did not set in the merge still appear in the fingerprint as `None`, so fields such as `profile` or `auth_type` affect the hash when they are present in the merged mapping.

Normalisation for hashing:

- Scalar values are stringified with `str()` and JSON-encoded in a canonical structure.
- Mapping keys are sorted so ordering does not affect the digest.
- `None` and `""` hash the same for a given attribute.

## Development

```bash
uv sync
uv build
uv run pytest
```
