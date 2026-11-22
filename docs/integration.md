# Consumer integration

Part of the [ns8-postgresql19c documentation](../README.md).

This is the contract between ns8-postgresql19c and the modules that keep
their data in it. The first planned consumer is ns8-warpgate, to run
Warpgate on several nodes against one clustered database.

## Summary

| What | Value |
|---|---|
| Discovery record | `module/<pg_id>/srv/tcp/postgresql19c` (no secrets) |
| Authorization label of the consumer | `postgresql19c@any:pgclient` |
| Get a database and credentials | action `grant-database` on `module/<pg_id>` |
| Disable or drop it | action `revoke-database`; login disabled automatically on `module-removed` |
| Endpoint | `host:port` of the client endpoint, on the consumer's node when there is one |
| Transport | plain TCP on the cluster VPN (WireGuard) or loopback, `sslmode=disable`, SCRAM-SHA-256 |
| Events | `postgresql19c-changed`, `postgresql19c-cluster-changed` |

## Authorization

In the consumer's `build-images.sh`:

```bash
--label="org.nethserver.authorizations=postgresql19c@any:pgclient"
```

The `pgclient` role of every ns8-postgresql19c instance contains only
`grant-database` and `revoke-database`. Their names do not start with
`get-`, `list-`, `show-` or `read-`: the builtin `reader` role cannot run
them. A module caller (`AGENT_TASK_USER` is `module/<id>`) always acts for
itself: the client ID is its module ID, and it cannot touch a database
created for another module (`database_owned_by_other`).

## Discovery

```
module/<pg_id>/srv/tcp/postgresql19c
  host        VPN address of the node, e.g. 10.5.4.2
  port        client endpoint, read-write (always the primary)
  ro_port     client endpoint, read-only (replicas in sync and the primary)
  node        NS8 node ID
  ha_mode     "standalone" or "cluster"
  cluster     "true" or "false"
  cluster_id  ID of the PostgreSQL cluster (the module ID of a standalone server)
  role        standalone, initial, join or witness
  version     19.0
  tls         "false"
```

Every instance of a cluster, witnesses included, publishes this record:
all of them serve the same databases through their endpoint.

```python
import agent
rdb = agent.redis_connect(use_replica=True)
for rec in agent.list_service_providers(rdb, "postgresql19c", "tcp"):
    print(rec["module_id"], rec["cluster_id"], rec["node"], rec["host"], rec["port"])
```

## grant-database

Creates the database if it is missing, creates or updates the caller's
login role, and returns new credentials. Run it as a task:

```python
import agent, agent.tasks
ret = agent.tasks.run(agent_id="module/postgresql19c1", action="grant-database", data={},
                      extra={"isNotificationHidden": True})
if ret["exit_code"] != 0:
    raise RuntimeError("grant-database failed")
creds = ret["output"]   # never log it
```

Input (all optional):

```json
{"database": "warpgate1", "client_id": "warpgate1"}
```

- `database`: default derived from the client ID (`my-app2` → `my_app2`).
  Lowercase letters, digits and `_` (at most 57); not `postgres`,
  `template*`, `pg_*` or the system roles.
- `client_id`: administrators only (`api-cli`); a module is always itself.
- `share`: administrators only, with `client_id`: give that client a login
  role on an existing database created for another client.

Roles:

- `<database>_owner` (no login) owns the database, its `public` schema and,
  through a per-database `SET role`, every object the clients create;
- each client has its own login role, named after the client
  (`warpgate1`), member of the owner role.

Sharing: a database belongs to the client that created it (comment
`ns8-client=<id>;ns8-app=<image>`). **Another module of the same
application** (same image name, e.g. `warpgate2` next to `warpgate1`) may
ask for the same `database` and gets its own login role on it: this is how
several instances of a clustered application share one database. Other
modules are refused (`database_owned_by_other`); an administrator can still
share with `share: true`.

Output:

```json
{
  "client_id": "warpgate1",
  "database": "warpgate1",
  "username": "warpgate1",
  "password": "<64 hexadecimal characters>",
  "host": "10.5.4.2",
  "port": 20005,
  "ro_port": 20006,
  "endpoints": [
    {"module_id": "postgresql19c1", "node": "1", "host": "10.5.4.1", "port": 20005, "ro_port": 20006},
    {"module_id": "postgresql19c2", "node": "2", "host": "10.5.4.2", "port": 20005, "ro_port": 20006}
  ],
  "ha_mode": "cluster",
  "cluster_id": "postgresql19c1",
  "tls": false,
  "sslmode": "disable",
  "url": "postgresql://warpgate1:<password>@10.5.4.2:20005/warpgate1?sslmode=disable",
  "openbao_path": "postgresql19c/postgresql19c1/client/warpgate1"
}
```

- `host`/`port`: the endpoint of the instance on the **caller's node** if
  the cluster has one there, otherwise the endpoint of the instance that
  answered. Every endpoint reaches the primary.
- **Every call rotates the password.** The old one stops working at once.
  Call it at first use and when the stored credentials are refused.
- The password is hexadecimal: it can go in a URL without escaping.
- The login role is not a superuser, cannot create databases or roles,
  and cannot connect to other databases.
- `openbao_path` only when OpenBao is on: the credentials are also stored
  there for OpenBao-aware applications ([openbao.md](openbao.md)).
- Store the password in a `0600` file of the consumer, never with
  `agent.set_env()`, in argv, in logs or in the consumer's own action
  output. Its `get-configuration` should return a `*_set` boolean.
- NS8 keeps the task output for a few hours under this module's task keys
  in the cluster Redis, readable by the caller and the cluster
  administrators only, not by other modules.
- Errors (exit code 3): `""` `not_configured`, `client_id`
  `client_id_mismatch` or `required`, `database` `invalid`,
  `database_owned_by_other`, `database_not_managed` (a database this module
  did not create), `login_role_owned_by_other`, `login_role_not_managed`.

## revoke-database

```json
{"database": "warpgate1", "drop": false}
```

Without `drop`, the caller's login role loses its login and its sessions
are closed (data kept, other clients unaffected). With `drop: true` (the
creator of the database or an administrator) the database, its owner role
and the login roles of its clients are dropped. Output:
`{"client_id", "database", "revoked", "dropped"}`. When a consumer module
is removed, the `module-removed` event disables its login role; the data
stays until an administrator drops it.

## Events

| Event | Payload | When |
|---|---|---|
| `postgresql19c-changed` | `{"module_id", "module_uuid", "cluster_id", "ha_mode"}` | the endpoint record of an instance changed (role, address, cluster) |
| `postgresql19c-cluster-changed` | `{"cluster_id", "module_id", "reason"}` | membership changed (internal; consumers may ignore it) |

A consumer should handle `postgresql19c-changed` and `module-removed` for
the instance it uses: re-read the records and switch to another endpoint
with the same `cluster_id` (the credentials stay valid on all of them).

## Failover as seen by a client

During a failover the endpoint closes the sessions to the old primary;
connections opened in the next 30 to 45 seconds fail or wait (connect
timeout 4 s per attempt). Use a connect timeout of a few seconds and
retries, and treat errors such as "terminating connection", "server closed
the connection" and "read-only transaction" as temporary.

## Warpgate

How Warpgate runs today (ns8-warpgate): one container, `unattended-setup`
writes `/data/warpgate.yaml` and a SQLite database in the `data` volume.
Warpgate's upstream image is built with the `postgres` feature
(`docker/Dockerfile`: `cargo build --features mysql,postgres,...`), and
`store.database_url` accepts a `postgres://` URL. Its connection code
(`warpgate-core/src/db/mod.rs`) passes the URL to sea-orm/sqlx: a single
host only, no `target_session_attrs`, a pool of 5 to 100 connections with
an 8 second connect timeout and connection lifetime. Hence the per-node
endpoint.

Plan for ns8-warpgate (not part of this module):

1. Label `postgresql19c@any:pgclient`; a `database` setting naming the
   ns8-postgresql19c instance (or cluster) to use, empty for SQLite.
2. `configure-module` calls `grant-database` with no input when it has no
   credentials (or they are refused), stores the password in
   `state/database.env` (0600), and renders
   `database_url: postgres://warpgate1:<password>@<host>:<port>/warpgate1?sslmode=disable`
   into `warpgate.yaml` (the file is in the data volume and must stay 0600);
   `host`/`port` are those returned, i.e. the endpoint on Warpgate's node.
3. Container network: the VPN address of the node is reachable from a
   rootless container (slirp4netns or pasta route it to the host). For the
   loopback address use `--network=slirp4netns:allow_host_loopback=true`
   and `10.0.2.2` (NethServer's ldapproxy documentation).
4. Several Warpgate instances on several nodes use the same database, each
   through the endpoint of its own node: every instance calls
   `grant-database` with the same `database` (for example the cluster name
   chosen in its settings, or the module ID of the first instance). The
   first creates it; the others are instances of the same application, so
   they get their own login role on it. Tables belong to the owner role,
   so all instances use them.
5. Handle `postgresql19c-changed` and `module-removed` as above.
6. Migration of the existing SQLite data: `warpgate copy-database
   <postgres URL>` (command of Warpgate 0.30) copies every table, then set
   `database_url` and restart.
7. Max connections: Warpgate pools up to 100 connections per instance; the
   cluster default `max_connections` is 300. Raise it for more instances.

Static credentials: Warpgate reads `database_url` at startup and cannot
renew leases, so it uses the static per-consumer credentials of
`grant-database`, not OpenBao dynamic database credentials.

## External PostgreSQL

A consumer that also accepts a PostgreSQL outside NethServer should ask for
the same fields (`host`, `port`, `database`, `username`, `password`,
`sslmode`) and run the same code path; a managed service with a failover
address, or a Patroni cluster behind a load balancer, looks the same.
