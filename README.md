# dbx-tools-config

Tiny wrapper around `databricks.sdk.config.Config` for **services that
build a `Config` per request** from caller-supplied inputs - MCP
servers, brokers, multi-tenant backends, sidecars, agent frameworks,
etc.

The Databricks SDK auto-discovers config from `os.environ` of the host
process, which is the wrong scope for a service serving many callers.
This module lets each request bring its own config-shaped inputs and
materialise a `Config` (or just a fingerprint) from them. A typical
mapping for an HTTP/MCP-style request:

| Source                                  | dbx-tools-config layer |
| --------------------------------------- | ---------------------- |
| Request headers (env-shaped)            | `env=`                 |
| POST body / RPC payload `Config` fields | `**kwargs`             |
| Pre-resolved `Config` baseline          | `config=`              |

Precedence is **`kwargs > env > config`** (last write wins). Every
layer is optional.

## API

Three public helpers, all with the same signature:

```python
def config_params(
    config: Config | None = None,
    env: Mapping[str, Iterable[str] | None] | None = None,
    **kwargs,
) -> dict[str, Any]: ...

def create_config(
    config: Config | None = None,
    env: Mapping[str, Iterable[str] | None] | None = None,
    **kwargs,
) -> Config: ...

def config_params_hash(
    config: Config | None = None,
    env: Mapping[str, Iterable[str] | None] | None = None,
    **kwargs,
) -> str: ...
```

- `config_params(...)` merges `config.as_dict()` + recognised `env` keys
  + `kwargs` into a single dict suitable for `Config(**...)`.
- `create_config(...)` is a one-liner for `Config(**config_params(...))`.
  **Expensive**: triggers `Config.__init__`'s host-metadata HTTP probe,
  `~/.databrickscfg` read and credential strategy bootstrap.
- `config_params_hash(...)` returns a SHA-256 hex digest of the merged
  kwargs after dropping fields in `_HASH_IGNORE_FIELDS`. **Cheap**:
  pure in-memory compute, no `Config` constructed. See
  [Hashing](#hashing).

## Install

Not published to PyPI - install directly from GitHub via a PEP 508
direct URL. Works with `pip`, `uv`, `poetry`, etc. - they all read
`[project].dependencies` from `pyproject.toml`.

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

Then install with whichever tool you use:

```bash
pip install .            # or: pip install -e .
uv sync                  # or: uv add 'dbx-tools-config @ git+https://github.com/reggie-db/dbx-tools-config'
```

## Usage

### Server-style: per-request Config from headers + body

```python
import dbx_tools_config
from databricks.sdk import WorkspaceClient

# An MCP-style handler. Headers carry env-shaped names, the body
# carries Config field overrides.
def handle_request(request):
    config = dbx_tools_config.create_config(
        env=request.headers,         # e.g. {"DATABRICKS_HOST": "...",
                                     #       "DATABRICKS_TOKEN": "..."}
        **request.json(),            # e.g. {"warehouse_id": "abc",
                                     #       "cluster_id": "xyz"}
    )
    return WorkspaceClient(config=config).do_work(...)
```

### Other shapes

```python
# From an arbitrary env-shaped mapping
config = dbx_tools_config.create_config(env={
    "DATABRICKS_HOST": "https://myworkspace.cloud.databricks.com",
    "DATABRICKS_TOKEN": "dapi...",
})

# From the process environment (single-tenant CLIs, scripts, tests)
import os
config = dbx_tools_config.create_config(env=os.environ)

# Kwargs always win over env
config = dbx_tools_config.create_config(
    host="https://override.cloud.databricks.com",
    env=client_env,
)

# Round-trip an existing Config (e.g. as a baseline)
config = dbx_tools_config.create_config(config=other_config, host="https://override...")

# Just the merged kwargs, without constructing a Config
kwargs = dbx_tools_config.config_params(config=other_config, env=client_env)
```

## Env value semantics

Each value in the `env` mapping may be:

| Value           | Behavior                                                         |
| --------------- | ---------------------------------------------------------------- |
| `str`           | Used directly.                                                   |
| `None`          | Sets the field to `None` (clears any baseline from `config=`).   |
| `Iterable[str]` | First element wins; matches multi-value HTTP / multidict frames. |
| empty iterable  | Field is left untouched.                                         |

## Env key resolution

Each key in the `env` mapping is matched against the SDK's declared
`ConfigAttribute.env` (and any `env_aliases`) on `Config`. Examples that
the SDK declares today:

- `DATABRICKS_HOST` -> `host`
- `DATABRICKS_TOKEN` -> `token`
- `DATABRICKS_CLUSTER_ID` -> `cluster_id`
- `DATABRICKS_OIDC_TOKEN_FILE` -> `oidc_token_filepath` (alias)
- `DATABRICKS_AZURE_RESOURCE_ID` -> `azure_workspace_resource_id`
- `ARM_TENANT_ID` -> `azure_tenant_id`
- `GOOGLE_CREDENTIALS` -> `google_credentials`

Keys that don't match a declared env name (or alias) are silently ignored.

> Note: this module does **not** perform string-to-bool/int/float coercion.
> Values are forwarded to `Config(**kwargs)` as-is and the SDK's descriptor
> `transform` (typically just the annotated type) does any conversion.
> Be aware that the SDK uses `bool(value)` for boolean fields, so the
> string `"false"` will resolve to `True`. Pass real Python booleans via
> `kwargs` if you care.

### Out of scope: ambient env vars

A handful of `databricks-sdk` features read env vars directly from
`os.environ` instead of going through `Config`:

- `DATABRICKS_RUNTIME_VERSION` (DBR detection / user-agent)
- `IS_IN_DB_MODEL_SERVING_ENV`, `IS_IN_DATABRICKS_MODEL_SERVING_ENV`,
  `DATABRICKS_MODEL_SERVING_HOST_URL`, `DB_MODEL_SERVING_HOST_URL`
  (model serving auto-auth)
- `ACTIONS_ID_TOKEN_REQUEST_TOKEN`, `ACTIONS_ID_TOKEN_REQUEST_URL`
  (GitHub Actions OIDC)
- `SYSTEM_ACCESSTOKEN`, `SYSTEM_*` (Azure DevOps OIDC)
- `AGENT` (user-agent)

Forwarding these through `dbx_tools_config.create_config(env=...)` has
no effect because they bypass `Config` entirely. If you need them in a
service context, set them on `os.environ` of the worker process before
constructing the SDK client.

## Hashing

`dbx_tools_config.config_params_hash(...)` returns a stable SHA-256 hex
digest of the resolved kwargs without constructing a `Config`. This
matters because `Config.__init__` is **not** free - it does (in order):

1. `_resolve_host_metadata` - HTTP `GET host/.well-known/databricks-config`
   to discover `account_id`, `workspace_id`, `cloud`, `discovery_url`.
2. `_known_file_config_loader` - reads `~/.databrickscfg` from disk if
   no auth is configured directly.
3. `_validate` - checks for conflicting auth methods.
4. `init_auth` - bootstraps the credential strategy (which itself may
   shell out to the Databricks CLI, fetch a token from disk, etc).

For a service that fans many requests over a small set of logical
identities, hashing first lets you cache (or rate-limit) clients
without paying any of the above per request:

```python
import dbx_tools_config
from databricks.sdk import WorkspaceClient

_clients: dict[str, WorkspaceClient] = {}

def client_for(request):
    key = dbx_tools_config.config_params_hash(env=request.headers, **request.json())
    client = _clients.get(key)
    if client is None:
        config = dbx_tools_config.create_config(env=request.headers, **request.json())
        client = _clients[key] = WorkspaceClient(config=config)
    return client
```

The digest deliberately ignores fields that don't change *which*
workspace / account is being addressed or *how* it's being authenticated:

| Group                   | Fields ignored                                              |
| ----------------------- | ----------------------------------------------------------- |
| Source / lookup         | `profile`, `config_file`, `databricks_cli_path`             |
| Derived during init     | `auth_type`, `databricks_environment`                       |

So two configs that resolve to the same identity but were loaded from
different `DATABRICKS_CONFIG_PROFILE` / `DATABRICKS_CONFIG_FILE` paths,
via a different CLI binary, or that happened to be tagged with a
different derived `auth_type`, fingerprint the same way.

Normalisation:

- Mapping keys are sorted (after JSON-encoding) so dict ordering does
  not affect the digest.
- `None` collapses with the empty string so an explicit `None` value
  hashes the same as an explicit `""`.
- Iterables (other than strings) preserve their order.
- Scalar values are stringified via `str()` and JSON-quoted before
  being streamed into the digest.

## Development

```bash
uv sync
uv build
uv run pytest
```
