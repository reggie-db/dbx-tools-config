# dbx-config

Tiny wrapper around `databricks.sdk.config.Config` that accepts an explicit
`env` mapping in addition to keyword arguments and coerces string values into
the types the SDK expects.

## Why

This is built for **services that generate `Config` objects from
client-supplied env vars forwarded as-is** — MCP servers, brokers,
multi-tenant backends, sidecars, agent frameworks, etc. The Databricks SDK
auto-discovers config from `os.environ` of the host process, which is the
wrong scope for a service serving many callers: each request needs its own
`Config` built from the caller's env, not from whatever the server happens to
be running with.

It's also handy any time you have an env-shaped mapping that isn't
`os.environ`:

- A `.env` file parsed into a dict.
- A workspace secret bundle.
- An HTTP request body or header set.
- A unit test fixture.
- A round-trip through `Config.as_dict()`.

In all of these cases the values are strings (because they came from an
env-shaped source) but the SDK attribute may be typed `bool`, `int`, or
`float`. `dbx_config.create()` covers both: pass any `Mapping[str, str]` as
`env=` and each key is mapped to the matching `Config` field and coerced to
the correct type before being forwarded to `Config(**kwargs)`.

## Install

```bash
uv add 'dbx-config @ git+https://github.com/reggie-db/dbx-config'
```

or in `pyproject.toml`:

```toml
dependencies = [
    "dbx-config @ git+https://github.com/reggie-db/dbx-config",
]
```

## Usage

```python
import dbx_config

# Service style: build a Config per request from the caller's env vars,
# forwarded as-is from an MCP client / HTTP request / RPC frame.
def handle_request(client_env: dict[str, str]):
    config = dbx_config.create(env=client_env)
    ...

# From an arbitrary mapping (values are strings)
config = dbx_config.create(env={
    "DATABRICKS_HOST": "https://myworkspace.cloud.databricks.com",
    "DATABRICKS_TOKEN": "dapi...",
    "DATABRICKS_DEBUG_HEADERS": "true",   # coerced to bool
    "DATABRICKS_RATE_LIMIT": "30",        # coerced to int
})

# From the process environment (single-tenant CLIs, scripts, tests)
import os
config = dbx_config.create(env=os.environ)

# Kwargs always win over env
config = dbx_config.create(
    host="https://override.cloud.databricks.com",
    env=client_env,
)

# Round-trip an existing Config through a dict
config = dbx_config.create(**other_config.as_dict())
```

## Env key resolution

Each key in the `env` mapping is resolved in this order:

1. Explicit `ConfigAttribute.env` declared on the SDK's `Config` class
   (and any `env_aliases`, e.g. `DATABRICKS_OIDC_TOKEN_FILE` ->
   `oidc_token_filepath`).
2. `DATABRICKS_<NAME>` -> `<name>` (e.g. `DATABRICKS_CLUSTER_ID` ->
   `cluster_id`).
3. `ARM_<NAME>` -> `azure_<name>` (e.g. `ARM_TENANT_ID` ->
   `azure_tenant_id`).

Keys that don't resolve to a known field are silently ignored.

## Type coercion

| Declared field type           | String value handling                                                                                                       |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `bool`                        | `1` / `true` / `yes` / `y` / `on` -> `True`, `0` / `false` / `no` / `n` / `off` -> `False` (case insensitive, whitespace stripped) |
| `int`                         | `int(value)`                                                                                                                |
| `float`                       | `float(value)`                                                                                                              |
| `str` / unknown               | passed through unchanged                                                                                                    |
| any nullable field with `""`  | `None`                                                                                                                      |

A coercion failure (`int("abc")`, an unrecognized bool token, etc.) leaves
the original string untouched so the SDK's own validation can surface the
error in context.

## Development

```bash
uv sync
uv build
```
