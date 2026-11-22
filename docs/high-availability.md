# High availability

Part of the [ns8-postgresql19c documentation](../README.md).

## Roles

| Role | Runs | Data | Notes |
|---|---|---|---|
| `standalone` | etcd (one voter), Patroni, endpoint | its own | a cluster of one member, cluster ID = its module ID |
| `initial` | same | its own | the same as standalone, but publishes its member record and admits approved instances |
| `join` | etcd, Patroni (replica), endpoint | a copy of the cluster | promoted automatically when the primary fails |
| `witness` | etcd, endpoint | none | the third vote of a two-node cluster |

The cluster ID is the module ID of the instance that created the cluster.
It stays the same when that instance leaves: any member can admit new
ones.

### Allowed changes

| From | To | Effect |
|---|---|---|
| none or `standalone` | any role | `standalone` and `initial` keep the data; `join` replaces it (refused while user databases exist: `instance_not_empty`) |
| `initial` alone | `standalone` | keeps the data |
| `initial`, `join`, `witness` with other members | `standalone` | **leaves the cluster**: hands the primary role over, leaves etcd, restarts empty with new secrets and a new CA |
| between cluster roles, or another cluster | - | refused (`leave_cluster_first`) |

## Quorum rules

etcd elects a leader and accepts writes only with a majority of its
**voters**. Patroni keeps its leader lock in etcd.

| etcd voters | Majority | Voters that may fail | Automatic failover |
|---|---|---|---|
| 1 (standalone) | 1 | 0 | no replica |
| 2 | 2 | **0** | no: losing either node stops failover |
| 3 | 2 | 1 | yes |
| 4 | 3 | 1 | yes (no better than 3) |
| 5 | 3 | 2 | yes |

- **Use three nodes**: three PostgreSQL members, or two PostgreSQL members
  and one **witness** on a third node (a small node is enough: etcd only).
- **Two voters never split brain**: when one is lost, the other has no
  majority. With `failsafe_mode`, the primary keeps running while every
  member it knows confirms it through the REST API; if the primary's own
  node is lost, nothing is promoted until it returns or an administrator
  recovers the cluster. The *Status* page and `get-cluster-status` warn
  (`below_quorum`, `even_voters`, `quorum_lost`).
- A joining member is an etcd **learner** (no vote) until it caught up;
  then it is promoted (by the joining member itself, and by the periodic
  check if needed). A join that fails half way leaves at most a learner,
  which does not change the majority.
- Several members on one node (`allow_same_node`) exist for tests only:
  such a cluster does not survive a node failure.

## Failure behaviour

| Event | What happens | Clients |
|---|---|---|
| Replica down | the primary goes on; the replica catches up (or is rebuilt) when it returns | nothing |
| Primary process crashes | Patroni restarts PostgreSQL in place | short reconnection |
| Primary node lost (3+ voters) | after the leader key expires (TTL 30 s) the healthiest replica within 1 MB of lag is promoted | the endpoints switch; about 30 to 45 seconds without writes |
| Old primary returns | `pg_rewind`, then it streams from the new primary | nothing |
| Network partition | only the side with the etcd majority keeps or elects a primary; the other side demotes itself | clients on the minority side get errors |
| etcd quorum lost, all PostgreSQL members up | failsafe mode: the primary keeps running, no failover possible | nothing |
| Graceful stop of the primary (update, leave, remove) | switchover to a healthy replica first | a short reconnection |

Replication is asynchronous by default: a failover can lose the
transactions committed on the old primary and not yet replicated (at most
`maximum_lag_on_failover`, 1 MB). With `replication_mode: sync`, Patroni's
synchronous mode makes one replica synchronous: a commit returns after
that replica has it, and only that replica can be promoted. It is not
strict: when no replica is available the primary keeps accepting writes.

Watchdog: a rootless container has no `/dev/watchdog`, so Patroni runs
without one. Patroni demotes a primary that cannot refresh its leader key
before the TTL expires.

## Membership

- **Discovery**: members publish `module/<id>/srv/tcp/postgresql19c-cluster`
  (VPN address, ports, node, cluster ID, role; no secret).
  `get-configuration` lists the clusters to join as `cluster_candidates`.
- **Approval**: on a member, the administrator allows each instance that
  will join (`set-peer-access`, the *Settings* page). The approval is bound
  to the module UUID of the peer, is not backed up and not cloned.
- **Join**: the new instance calls `admit-member` on the members (role
  `pgpeer`). The member checks the caller's identity (`AGENT_TASK_USER`),
  the approval, that the announced address is the VPN address of the
  caller's node, and that the node has no member yet; adds it as an etcd
  learner; returns the etcd initial cluster and, unless the joiner read
  them from OpenBao, the cluster secrets. The joiner stores them in `0600`
  files, starts etcd, promotes itself, then starts Patroni, which builds the
  replica with `pg_basebackup` from the primary.
- **Events**: `postgresql19c-cluster-changed` (member joined, left,
  configured, removed) makes every member refresh its endpoint and etcd
  membership; `module-removed` makes the primary remove the etcd member of
  a removed module (also when its node died).
- **Periodic check** (`pgha-reconcile.timer`, 5 minutes): publishes the
  records, reloads the endpoint when the member list changed, promotes a
  pending learner, removes etcd members of removed modules (on the
  primary), pushes shared secrets to OpenBao. It acts only on drift.
- **Removal**: `remove-module` runs `destroy-module`: switchover if this
  member is the primary, Patroni stopped, etcd member removed.

## Settings of the whole cluster

`replication_mode` and `max_connections` live in Patroni's dynamic
configuration (the DCS). Saving them on any PostgreSQL member applies them
to all; `max_connections` needs a restart, done member by member by
`patronictl restart --pending`. A joining member takes the cluster values.
