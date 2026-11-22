# Architecture

Part of the [ns8-postgresql19c documentation](../README.md).

## Components of an instance

Each instance (normally one per node) runs, as rootless Podman containers
of its module user with host networking:

| Unit | Container | Image | Role |
|---|---|---|---|
| `etcd.service` | `etcd` | runtime image | one member of the etcd store (Patroni's DCS) |
| `patroni.service` | `patroni` | runtime image | Patroni, which starts PostgreSQL 19 as primary or replica |
| `pgproxy.service` | `pgproxy` | HAProxy 3.2 LTS | the client endpoint |
| `pgadmin.service` | `pgadmin` | pgAdmin 9.19 | optional, behind Traefik |
| `pgha-reconcile.timer` | - | - | membership check every 5 minutes |

A witness runs `etcd` and `pgproxy` only.

```
          node 1                          node 2                          node 3
 ┌──────────────────────────┐   ┌──────────────────────────┐   ┌──────────────────────────┐
 │ postgresql19c1 (initial) │   │ postgresql19c2 (join)    │   │ postgresql19c3 (join or  │
 │                          │   │                          │   │   witness)               │
 │  pgproxy :EP ─┐          │   │  pgproxy :EP ─┐          │   │  pgproxy :EP ─┐          │
 │               ▼ /primary │   │               ▼          │   │               ▼          │
 │  patroni ── PostgreSQL ◄─┼───┼── streaming replication ─┼───┼─► PostgreSQL ── patroni  │
 │     │       (primary)    │   │      (replica)           │   │     (replica)    │       │
 │     ▼                    │   │                          │   │                  ▼       │
 │  etcd ◄──────────────────┼───┼──── etcd (raft, mTLS) ───┼───┼──────────────► etcd     │
 └──────────────────────────┘   └──────────────────────────┘   └──────────────────────────┘
        all traffic between nodes: the NethServer cluster VPN (WireGuard, trusted zone)
 consumers on node N use 10.5.4.N:EP (or 127.0.0.1:EP); the endpoint routes to the primary
```

## Choices

### Failover engine: Patroni with etcd

- **Patroni** (4.1.5) is the reference failover manager for PostgreSQL:
  leader election through a DCS, automatic failover, `pg_rewind` of a
  former primary, replicas built with `pg_basebackup`, synchronous mode,
  REST health checks for load balancers, graceful switchover.
- **DCS: etcd v3** (3.6.17), one member inside each instance, like the
  integrated Raft storage of ns8-openbao: no external service to install.
  Patroni's built-in Raft DCS (pysyncobj) is marked *deprecated* in the
  Patroni 4.1.5 documentation, so it is not used. Consul would be an
  extra agent per node with no benefit here.
- Alternatives considered: **pg_auto_failover** needs a separate monitor
  node, which is itself a single point of failure unless replicated;
  **repmgr** has no consensus store (fencing and split-brain avoidance are
  left to scripts); **CloudNativePG** and Stolon need Kubernetes.

### Quorum

etcd needs a majority of its voters. See
[high availability](high-availability.md#quorum-rules): three voters on
three nodes; a **witness** role (etcd only) gives the third vote to a
two-node PostgreSQL cluster. A joining member is added as an etcd
**learner** (non-voting) and promoted once it caught up, so a join that
never completes cannot break the quorum. Patroni runs with
`failsafe_mode: true`: when etcd is unavailable, the primary keeps running
only while every member confirms it through the REST API, which never
allows two primaries.

### Client endpoint

Warpgate, the first planned consumer, opens its database with sqlx through
sea-orm: `connect_to_db()` parses `database_url` with `url::Url` and hands
it to sqlx (`warpgate-core/src/db/mod.rs`, release 0.30.4). sqlx does not
support libpq multi-host URLs nor `target_session_attrs`, so a client
cannot find the primary by itself. Every instance therefore runs a
**per-node L4 proxy**, as NethServer's own ldapproxy does for LDAP:

- `primary` listener (`ENDPOINT_PORT`): HAProxy sends connections to the
  member whose Patroni answers `GET /primary` with 200. On a failover the
  old primary's checks fail and its sessions are shut down
  (`on-marked-down shutdown-sessions`); clients reconnect and reach the new
  primary.
- `readonly` listener (`ENDPOINT_RO_PORT`): any running member (replicas
  and the primary) with less than 16 MB of replication lag
  (`GET /read-only?lag=16MB`), least connections first.

The member list comes from the NethServer service records and is reloaded
(`SIGUSR2`, no dropped connections) when members join or leave.

### Networking

- Host networking; the node cluster VPN address comes from Redis
  (`node/<id>/vpn` `ip_address`), not from an environment variable.
- PostgreSQL, the endpoint and the etcd client port listen on `127.0.0.1`
  and the VPN address; Patroni's REST API and the etcd peer port on the VPN
  address. By default nothing listens on public addresses and no firewall
  port is opened: the VPN network is in the `trusted` zone of every node.
  Direct access from outside is opt-in per instance (`node:fwadm`), see
  [network access](network-access.md).
- Eight TCP ports per instance (`org.nethserver.tcp-ports-demand=8`), all
  allocated when the module is added:

| Index | Variable | Use | Listens on |
|---|---|---|---|
| 1 | `PG_PORT` | PostgreSQL | loopback, VPN |
| 2 | `PATRONI_PORT` | Patroni REST API | VPN |
| 3 | `ETCD_CLIENT_PORT` | etcd client (mTLS) | loopback, VPN |
| 4 | `ETCD_PEER_PORT` | etcd peer (mTLS) | VPN |
| 5 | `ENDPOINT_PORT` | client endpoint, read-write | loopback, VPN |
| 6 | `ENDPOINT_RO_PORT` | client endpoint, read-only | loopback, VPN |
| 7 | `PGADMIN_PORT` | pgAdmin (Traefik) | loopback |
| 8 | `PUBLIC_PORT` | public endpoint, TLS only, when direct access is on ([network access](network-access.md)) | all IPv4 addresses; firewall opened per settings |

### Security

- **etcd**: mutual TLS for clients and peers, with certificates signed by a
  CA private to the PostgreSQL cluster (Patroni authenticates with its
  member certificate). Another module on the VPN cannot read or write the
  store, which would let it elect a primary.
- **Patroni REST**: health checks (`GET`) are open; every unsafe call
  (switchover, restart, configuration) needs the REST password.
- **PostgreSQL**: `password_encryption = scram-sha-256` and a SCRAM-only
  `pg_hba` (md5 is deprecated by PostgreSQL 19) for every user: superuser,
  replication, rewind, consumers. The local socket (peer authentication) is
  reachable only inside the Patroni container. Consumers cannot connect to
  the `postgres` and `template1` databases.
- **Secrets**: the superuser, replication, rewind and REST passwords and
  the CA key are cluster-wide and live in `0600` files of the state
  directory (and optionally OpenBao). The rendered `patroni.yml` is `0600`
  and regenerated at every start. Nothing secret goes to the module
  environment, Redis keys, argv, logs or reader actions. Peers receive the
  cluster secrets only through the guarded `admit-member` action, after
  the administrator approved them (`set-peer-access`), or from OpenBao.
- Consumer passwords are sent to PostgreSQL as SCRAM verifiers computed by
  the module: the clear text never reaches PostgreSQL's logs.

### Files

| Path | Content |
|---|---|
| `state/secrets.env` | cluster and pgAdmin passwords (0600, backed up) |
| `state/tls/` | cluster CA and member certificate (0600, not backed up) |
| `state/etcd/`, `state/patroni/`, `state/pgproxy/` | configuration rendered at every start |
| `state/members.json` | last member list, used when Redis is not reachable at start |
| `state/peer-access/` | approvals of peers (not backed up, not cloned) |
| `state/openbao.env` | OpenBao address and token (0600, not backed up) |
| volumes `pgdata`, `etcd-data`, `pgadmin-data` | data |

### PostgreSQL 19 notes

- `max_locks_per_transaction` is set to 128 (the PostgreSQL 19 default)
  explicitly in the cluster parameters: Patroni requires equal values on
  every member and its own default is 64.
- `wal_sender_shutdown_timeout = 30s` (new in 19): a stuck replica cannot
  block the shutdown of the primary during a switchover or restart forever.
- `io_workers` (removed in 19) is never set; JIT off, lz4 TOAST
  compression and `log_lock_waits` on are the PostgreSQL 19 defaults and
  left as they are.
- The data directory is `/var/lib/postgresql/19/docker` on the `pgdata`
  volume, as in the official image; `pg_hosts.conf` (SNI) would live there
  too, but client TLS is not offered (the VPN encrypts the traffic).
