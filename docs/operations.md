# Operations

Part of the [ns8-postgresql19c documentation](../README.md).

## Inspect

```bash
api-cli run module/postgresql19c1/get-cluster-status | jq
runagent -m postgresql19c1 podman exec -u postgres patroni patronictl list
runagent -m postgresql19c1 podman exec etcd etcdctl --endpoints=https://127.0.0.1:$ETCD_CLIENT_PORT member list -w table
runagent -m postgresql19c1 podman exec -u postgres patroni psql       # superuser shell (local socket)
journalctl -t postgresql19c1 -f                                       # or the System logs page
```

`etcdctl` inside the `etcd` container uses the member certificate
(`ETCDCTL_CACERT`, `ETCDCTL_CERT`, `ETCDCTL_KEY` are set there).

## Backup and restore

What a backup holds (`etc/state-include.conf`):

| Path | |
|---|---|
| `state/postgresql.dump` | `pg_dumpall` (plain SQL) made by `bin/module-dump-state` **on the current primary only**; replicas and witnesses write a one-line marker instead, so a cluster does not store its data once per member |
| `state/secrets.env` | local copy of the passwords (0600); lets a restore work without OpenBao |
| `state/secrets/clients/` | consumer credentials copied from OpenBao, if any |
| `volumes/pgadmin-data` | pgAdmin settings |
| `state/environment` | settings |

The dump leaves out the system roles (`postgres`, `replicator`,
`rewind_user`): Patroni creates them in the restoring cluster with its own
passwords. Consumer roles keep their SCRAM verifiers, so their passwords
still work after a restore.

**Back up a cluster**: schedule the backup of every member. Whichever is
primary at backup time writes the dump; the others back up their settings.
To keep one dump per day whatever the failovers, schedule all members.

**Restore** (any backup, standalone or member): the restored instance
always comes back as a **new standalone server** (its own cluster ID, a
new CA, fresh etcd data) and loads the dump. A backup of a replica or a
witness has no dump: restore the backup of the member that was primary.
Then rebuild the cluster around it:

1. Restore the primary's backup (on any node): standalone server with the
   data.
2. Make it the first member: `{"role": "initial"}`.
3. On it, allow the new members (`set-peer-access`), and on each other node
   install an instance and join it (`{"role": "join", "cluster_id": ...}`).
   Their data is copied from the new primary.
4. Consumers: `postgresql19c-changed` tells them that the endpoint moved;
   the restored roles keep their passwords, or they call `grant-database`
   again.

**Clone** (move to another node): the clone gets a live copy of the data,
becomes a standalone server of its own (etcd data wiped, new CA and
secrets for its new cluster), and starts the copied data as primary
(tested: Patroni adopts an existing data directory with an empty store).
Peer approvals are not copied.

## Updates

`update-module` runs `update-module.d/20restart` (`bin/pgha-restart`): if
the instance is the primary and a healthy replica exists, it switches the
primary role over first, then restarts its units with the new images.
Update the members **one at a time**, replicas first. Patroni keeps the
data directory; a replica that missed WAL is rebuilt automatically.

PostgreSQL **major upgrades** (19 → 20) are not handled in place: create a
new module for the next major version and dump/restore (see below).

## Migrating from another PostgreSQL

PostgreSQL 19 needs a dump/restore (or `pg_upgrade`) from older versions:

- Dump with the **PostgreSQL 19 `pg_dump`** when possible (the
  `postgres:19.0` image): PostgreSQL 19 forces
  `standard_conforming_strings` on, and dumps of older servers made with it
  off do not load.
- From stephdl's `ns8-postgresql18` (or 16/14): it backs up with
  `pg_dumpall`. Restore its databases into this module per consumer:
  create the consumer database with `grant-database`, then
  `pg_restore`/`psql` into it as the superuser through the endpoint, or
  feed the dump to `podman exec -i -u postgres patroni psql` on the
  primary.
- From an application with an embedded PostgreSQL 16/17 (or SQLite, as
  Warpgate today): use the application's own migration (`warpgate
  copy-database <url>`), or `pg_dump --no-owner` from the old server and
  restore as the consumer's login role.
- `pg_upgrade` refuses clusters with `btree_gist` indexes on `inet`/`cidr`
  and object names containing CR or LF; after an upgrade or a restore,
  reindex full-text search and `pg_trgm` indexes
  (`REINDEX DATABASE <db>` is simplest).
- Logical replication can be used for a low-downtime migration: since
  PostgreSQL 19 a server with `wal_level=replica` can enable logical
  decoding without a restart (check `effective_wal_level`). Not automated
  by the module.

## Failover test procedure

On a cluster of three voters (three members, or two members and a
witness), with a test client:

1. Check health: `get-cluster-status` shows a leader, `quorum_ok: true`,
   every replica `streaming`, `failure_tolerance: 1`.
2. Get credentials and write through the endpoint of a non-primary node:

   ```bash
   umask 077; api-cli run module/postgresql19c2/grant-database --data '{"client_id": "failtest"}' > /root/g.json
   ```

   then use `psql "$(jq -r .url /root/g.json)"` from a container on that
   node, or the test helper `tests/pg-probe.py`.
3. **Crash the primary** (on its node, as root):

   ```bash
   runagent -m <primary> podman kill --signal=KILL patroni
   runagent -m <primary> systemctl --user stop patroni.service
   ```

   or power the node off. Within about 30 to 45 seconds another member is
   the leader (`get-cluster-status` on a surviving member), and writes
   through **any** endpoint succeed again.
4. **Bring it back**: `systemctl --user start patroni.service` (or boot the
   node). It rewinds and streams from the new primary; the status shows it
   as a replica, lag 0.
5. **Planned switchover**: `runagent -m <primary> pgha-restart` (or
   `patronictl switchover`) moves the primary role with a few seconds of
   reconnection.
6. **Quorum loss**: stop the etcd service of two of three voters. The
   primary keeps running (failsafe mode) while every PostgreSQL member
   answers; no failover can happen; the status warns `quorum_lost`. Start
   them again.
7. Clean up: `api-cli run module/postgresql19c2/revoke-database --data
   '{"client_id": "failtest", "drop": true}'`, `rm /root/g.json`.

The CI runs the same steps on four instances of one test node
(`tests/20_cluster.robot`), and `tests/container/cluster_test.py` runs them
on three Docker network namespaces.

## A member is lost for good

Remove its module (`remove-module --no-preserve <id>`, also when its node
is gone): the `module-removed` event makes the primary remove its etcd
member, and the endpoints drop it. Install a new instance and join it.

## A cluster lost its quorum

When a majority of the etcd voters is gone for good (for example two of
three nodes destroyed), the store cannot elect a leader and nothing can be
removed from it. Recover from the survivor that has the most recent data:

1. On the surviving member, make it a standalone server of its own by the
   same path as a clone: stop its units, wipe its etcd data, give it a new
   cluster ID, and start:

   ```bash
   runagent -m postgresql19c2
   systemctl --user stop patroni.service etcd.service
   touch "$AGENT_STATE_DIR/etcd/reset-data"   # wiped by the etcd container at start
   ```

   then edit `state/environment`: `PG_ROLE=standalone`,
   `PG_CLUSTER_ID=<this module ID>`, `PG_ETCD_INITIAL_STATE=new`,
   `PG_ETCD_INITIAL_CLUSTER=` and start `etcd.service` and
   `patroni.service`. Patroni finds an empty store and starts the existing
   data as primary (also from a replica's data). Patroni runs with
   `failsafe_mode`, so the old primary, if it still runs somewhere cut off,
   demotes itself when it cannot confirm its role with every member.
2. Remove the modules of the lost members, rebuild the cluster as after a
   restore (`initial`, approvals, joins).

An automated `force` option for this is a documented follow-up.

## Node VPN address change

The `vpn-changed` event re-renders and restarts the units of the node with
the new address and updates the member's etcd peer URL through its peers.
If that fails (no quorum), use the recovery above.

## Troubleshooting

| Symptom | Look at |
|---|---|
| `not_admitted` when joining | the instance is not allowed on any member (`set-peer-access`), or the cluster has no etcd quorum |
| a replica stays `creating replica` | the base backup is running (large database), or the primary's `pg_hba`/password: Patroni logs |
| `learner_pending` stays | the learner could not catch up: etcd logs of the member; the periodic check retries the promotion |
| `no_leader` | `patronictl list`, Patroni logs; etcd quorum |
| clients get "read-only transaction" | they use the read-only port, or connected during a failover: retry |
| endpoint refuses connections | `pgproxy.service` logs; `state/pgproxy/haproxy.cfg` lists the members |
