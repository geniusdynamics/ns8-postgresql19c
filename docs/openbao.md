# OpenBao / Vault

Part of the [ns8-postgresql19c documentation](../README.md).

## SecretManager

Every secret goes through `imageroot/pypkg/pgha_secrets.py`
(`SecretManager.get_secret(name)`, `set_secret(name, value, local=True)`,
`list()`), with two backends:

| Backend | Where | Role |
|---|---|---|
| local (always on) | `state/secrets.env` and `state/tls/ca.*` (0600) | the operational copy: units start from it |
| OpenBao (opt-in) | KV version 2: `<mount>/data/<path>/<cluster_id>/<name>`, field `value` | source of truth and sync point of the members of one cluster |

| Secret | Name | Shared by the cluster | Local copy |
|---|---|---|---|
| PostgreSQL superuser password | `PG_SUPERUSER_PASSWORD` | yes | always |
| replication password | `PG_REPLICATION_PASSWORD` | yes | always |
| `pg_rewind` password | `PG_REWIND_PASSWORD` | yes | always |
| Patroni REST password | `PATRONI_REST_PASSWORD` | yes | always |
| cluster CA (certificate, key) | `CA_CERT`, `CA_KEY` | yes | always |
| pgAdmin password | `PGADMIN_PASSWORD` | no | always |
| consumer credentials | `client/<login>` | - | no: OpenBao only, written by `grant-database` when OpenBao is on |

Consumer passwords are otherwise never stored: PostgreSQL keeps their SCRAM
verifiers and the clear text exists only in the `grant-database` output.
With OpenBao on, they are also published in OpenBao for applications that
read their credentials there.

## Boot without OpenBao

**PostgreSQL and Patroni never wait for OpenBao.** The bootstrap secrets
(superuser, replication, rewind, REST, CA) always have a local 0600 copy,
and the units render their configuration from it. After a full reboot of
the NethServer cluster, PostgreSQL starts and members rejoin while OpenBao
is still sealed. The reconcile timer (every 5 minutes) pushes to OpenBao
the shared secrets written while it was unavailable. A value that differs
in OpenBao is never overwritten nor adopted silently: the instance keeps
its local copy and logs a warning (`openbao_secret_conflict`).

## Turn it on

OpenBao page, or (the role must be given, as for any configure-module):

```bash
api-cli run module/postgresql19c1/configure-module --data '{
  "role": "initial",
  "vault_enabled": true,
  "vault_address": "http://10.5.4.1:20004",
  "vault_token": "<token>",
  "vault_mount": "secret",
  "vault_path": "postgresql19c"
}'
```

- `VAULT_ADDR` and `VAULT_TOKEN` go to `state/openbao.env` (0600); the
  token is never returned (`get-configuration` gives `vault_token_set`),
  never in Redis, argv or logs. It needs read and write access to
  `<mount>/data/<path>/*` (and `metadata` for deletion).
- Turning it on checks that OpenBao answers (`openbao_unreachable`,
  `openbao_denied`) and that it does not already hold different values for
  this cluster's secrets (`openbao_secret_conflict`), then copies them.
- Discovery: ns8-openbao publishes `module/<id>/srv/http/openbao` when its
  *Cluster network API* is on. `list-openbao-servers` returns those
  (`servers`) and the other OpenBao instances (`unpublished`, the UI says
  which setting to turn on); *Other OpenBao or Vault* takes any URL. Same
  as ns8-kumomta.

## Joining with OpenBao

Turn OpenBao on with the same mount and path on the joining instance (in
the same `configure-module` call as `"role": "join"`). If OpenBao holds the
secrets of the cluster, the joiner reads them there and asks
`admit-member` for its etcd membership only (`want_secrets: false`). If
OpenBao is unavailable, the peer-to-peer path is the fallback: the
approved member returns the secrets in the task output.

## Turn it off

As in ns8-kumomta:

1. `list-openbao-only-secrets` (reader) lists the secrets that exist only
   in OpenBao (names and paths, whether the token can read each one; never
   values).
2. `copy-openbao-to-local` (administrators) copies them to local 0600
   files (`state/secrets/clients/`).
3. `configure-module` with `"vault_enabled": false` (and optionally
   `"vault_copy_to_local": true` to copy in the same call). Items left only
   in OpenBao are reported as the warning `openbao_items_unavailable`.

The OpenBao page does this for you. Bootstrap secrets are never in that
list: they always have a local copy.

## Backup

The local copy (`state/secrets.env`, `state/secrets/clients/`) is in the
backup, so a restore works without OpenBao. The token file is not backed
up: a restored instance comes back with OpenBao off.

## Tokens and AppRole

The token is pasted by the administrator, as in ns8-kumomta. ns8-openbao
offers no guarded action to provision a policy and a token (or an AppRole)
for another module today (its actions: configure-module, configure-plugins,
status and listing actions). When it does, this module should request a
dedicated token through it instead. Until then, create a policy limited to
the path, for example:

```hcl
path "secret/data/postgresql19c/*"     { capabilities = ["create", "read", "update"] }
path "secret/metadata/postgresql19c/*" { capabilities = ["read", "list", "delete"] }
```

and a periodic token for it: `bao token create -policy=postgresql19c -period=768h`.

## Follow-up: database secrets engine

Not implemented. Plan: an administrator action
`register-openbao-database-engine` creates a dedicated PostgreSQL role
(`LOGIN CREATEROLE`, not superuser, SCRAM password generated and sent only
to OpenBao), then configures
`<db mount>/config/<cluster_id>` with `plugin_name:
postgresql-database-plugin`, `connection_url:
postgresql://{{username}}:{{password}}@<VPN host>:<endpoint port>/postgres?sslmode=disable`
(the endpoint follows the primary), `allowed_roles`, and rotates its root
credential so the module no longer knows it. Applications that support
OpenBao then request short-lived credentials. Warpgate cannot renew leases
(it reads `database_url` at startup) and keeps the static credentials of
`grant-database`.
