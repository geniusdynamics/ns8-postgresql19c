# ns8-postgresql19c

[PostgreSQL 19](https://www.postgresql.org/docs/19/release-19.html) for
other NethServer 8 modules, as a standalone server or as a highly
available cluster spread over the nodes of the NethServer cluster, one
instance per node.

- **Standalone**: one server. It becomes the first member of a cluster
  later, in place, with its data.
- **Cluster**: [Patroni](https://patroni.readthedocs.io/) runs PostgreSQL
  on every member and fails over through an
  [etcd](https://etcd.io/) store whose members run inside the instances
  (no external dependency). Streaming replication, `pg_rewind`, automatic
  failover, graceful switchovers.
- **One stable endpoint**: every instance runs a client endpoint
  (HAProxy, checked against the Patroni REST API) that always reaches the
  current primary, plus a read-only endpoint. Consumers use the endpoint
  of their own node and never follow a failover themselves.
- **Consumer contract**: a module gets its own database and SCRAM
  credentials from a guarded action; nothing secret goes through Redis.
- Optional OpenBao for the shared secrets, optional pgAdmin.

Documentation:

- [Architecture](docs/architecture.md): components, ports, choices
  (Patroni + etcd, endpoint, networking, security)
- [High availability](docs/high-availability.md): roles, quorum rules,
  joining and leaving, failure behaviour
- [Consumer integration](docs/integration.md): the contract for modules
  that use PostgreSQL, with the Warpgate plan
- [Operations](docs/operations.md): backup and restore, updates,
  migration from other PostgreSQL modules, failover test procedure,
  troubleshooting
- [OpenBao](docs/openbao.md): the SecretManager, OpenBao mode, boot
  without OpenBao
- [Network access](docs/network-access.md): pgAdmin, direct database
  access from outside the cluster (firewall, allowlist, TLS)
- [Development](docs/development.md): tests and the container test

## Versions

| Component | Version | Where it is pinned |
|---|---|---|
| PostgreSQL | 19.0 (`docker.io/library/postgres:19.0-trixie`) | `server/Containerfile` |
| Patroni | 4.1.5 (PyPI) | `server/Containerfile` |
| etcd | v3.6.17 (GitHub release, SHA256 checked) | `server/Containerfile` |
| HAProxy | 3.2.27 LTS (`docker.io/library/haproxy:3.2.27-alpine`) | `build-images.sh` |
| pgAdmin | 9.19.0 (`docker.io/dpage/pgadmin4:9.19.0`) | `build-images.sh` |

Renovate tracks all of them. The runtime image
`ghcr.io/geniusdynamics/postgresql19c-server` is built by
`build-images.sh` and published with the module image, with the same tag.

**Patroni and PostgreSQL 19**: PostgreSQL 19.0 was released on
2026-11-19. The newest Patroni release, 4.1.5 (2026-08-12), lists
compatibility up to PostgreSQL 18; the PostgreSQL 19 specific changes
(`pg_hosts.conf`, `CHECKPOINT (FLUSH_UNLOGGED)`) are merged in Patroni's
main branch but not released yet. The parts Patroni touches were tested
with PostgreSQL 19.0 by `tests/container/cluster_test.py`: bootstrap
(initdb), `pg_basebackup` replicas, recovery configuration, version
detection, failover, `pg_rewind` of the old primary, switchover. Renovate
proposes the Patroni release with official support when it is out.

## Install

    add-module ghcr.io/geniusdynamics/postgresql19c:latest 1

Output example:

    {"module_id": "postgresql19c1", "image_name": "postgresql19c", "image_url": "ghcr.io/geniusdynamics/postgresql19c:latest"}

Nothing runs until a role is chosen.

## Configure

The *Settings* page has a role picker. From the command line:

```bash
# Standalone server
api-cli run configure-module --agent module/postgresql19c1 --data '{"role": "standalone"}'

# The same instance becomes the first member of a cluster (data kept)
api-cli run configure-module --agent module/postgresql19c1 --data '{"role": "initial"}'

# On the first member, allow the instances that will join
api-cli run set-peer-access --agent module/postgresql19c1 --data '{"peer": "postgresql19c2", "allowed": true}'
api-cli run set-peer-access --agent module/postgresql19c1 --data '{"peer": "postgresql19c3", "allowed": true}'

# Replicas on nodes 2 and 3
api-cli run configure-module --agent module/postgresql19c2 --data '{"role": "join", "cluster_id": "postgresql19c1"}'
api-cli run configure-module --agent module/postgresql19c3 --data '{"role": "join", "cluster_id": "postgresql19c1"}'
```

| Field | Description |
|---|---|
| `role` | `standalone`, `initial` (first member of a new cluster), `join` (PostgreSQL replica) or `witness` (etcd vote only) |
| `cluster_id` | with `join` or `witness`: the cluster, from `get-configuration` `cluster_candidates` |
| `replication_mode` | cluster-wide: `async` (default) or `sync` |
| `max_connections` | cluster-wide, default 300 |
| `pgadmin_enabled`, `host`, `pgadmin_path`, `lets_encrypt`, `pgadmin_email`, `pgadmin_ip_allowlist` | optional pgAdmin behind Traefik, HTTPS only |
| `public_access_enabled`, `public_access_allowlist` | direct access from outside the cluster, TLS only, per instance, see [network access](docs/network-access.md) |
| `vault_*` | optional OpenBao, see [docs/openbao.md](docs/openbao.md) |
| `allow_same_node` | with `initial`: several members on one node. **Tests only** |

Quorum: a cluster needs **three etcd voters on three nodes** to fail over
automatically: three PostgreSQL members, or two members and a witness. Two
voters never split brain, but stop failing over (and stop writes after a
while) when either one is lost. See [high availability](docs/high-availability.md).

## Actions

| Action | Who | Purpose |
|---|---|---|
| `configure-module` | administrators | role and settings |
| `set-peer-access` | administrators | allow an instance to join this cluster |
| `reveal-pgadmin-password` | administrators | initial pgAdmin password |
| `copy-openbao-to-local` | administrators | copy OpenBao-only secrets to local files |
| `register-openbao-database-engine` | administrators | OpenBao database secrets engine connection |
| `get-configuration`, `get-cluster-status`, `list-databases`, `list-openbao-servers`, `list-openbao-only-secrets` | readers | settings, health, databases (no secrets) |
| `grant-database`, `revoke-database` | role `pgclient` (`postgresql19c@any:pgclient`) | a consumer's database and credentials, see [integration](docs/integration.md) |
| `admit-member`, `reconcile-cluster` | role `pgpeer` (`postgresql19c@any:pgpeer`) | between instances of this module |

## What is implemented

- Standalone, cluster (initial, join, witness), standalone to cluster in
  place, leaving a cluster, member removal, etcd learners, periodic
  reconciliation (events and a 5-minute timer)
- Client endpoint on every instance, read-write and read-only
- Consumer contract (`grant-database`, `revoke-database`, events)
- Backup (dump on the primary only), restore and clone as a new
  standalone server, rolling restart with switchover on update
- SecretManager (local 0600 files) and opt-in OpenBao mode (KV v2,
  local cache, sync on the reconcile timer, copy to local when turning
  it off)
- Network access per instance: pgAdmin route (host or path, allowlist),
  direct access through a TLS-only public endpoint, firewall opening or
  source rich rules, the same allowlist in the proxy
- UI: Settings (with Network access), Status (members, roles, lag, quorum,
  databases), OpenBao

- `register-openbao-database-engine`: OpenBao database secrets engine
  connection for applications that use dynamic credentials (unit tested
  against a stub only)

Documented follow-ups (not implemented): AppRole or token provisioning by
ns8-openbao (it offers no such action yet), client
TLS, PostgreSQL major upgrades in place, automatic disaster recovery of a
cluster that lost its quorum (manual procedure in
[operations](docs/operations.md#a-cluster-lost-its-quorum)).

## Uninstall

    remove-module --no-preserve postgresql19c1

A cluster member hands the primary role over and leaves the etcd store
before it is removed.

## Testing

Unit tests (no NethServer needed):

    python3 -m unittest discover -s tests/unit

Container test of the runtime image (Docker):

    docker build -f server/Containerfile -t pg19c-server:test server
    python3 tests/container/cluster_test.py

Robot Framework suites on a test node: see [development](docs/development.md).

## UI translation

Translated with [Weblate](https://hosted.weblate.org/projects/ns8/).
