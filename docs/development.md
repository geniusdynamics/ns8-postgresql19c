# Development

Part of the [ns8-postgresql19c documentation](../README.md).

## Layout

| Path | |
|---|---|
| `server/` | runtime image: `Containerfile` (PostgreSQL 19 + Patroni + etcd) and the container entry points |
| `imageroot/pypkg/pgha.py` | rendering (pure functions), membership, status |
| `imageroot/pypkg/pgha_secrets.py` | SecretManager, OpenBao backend |
| `imageroot/pypkg/pgha_peer_access.py` | approvals of peers (set-peer-access) |
| `imageroot/actions/`, `events/`, `bin/`, `systemd/user/` | NS8 actions, event handlers, helpers, units |
| `tests/unit/` | unit tests (stdlib unittest, fake agent, fake OpenBao) |
| `tests/container/cluster_test.py` | three-member test of the runtime image with Docker |
| `tests/*.robot` | Robot Framework suites run on an NS8 node |

## Unit tests

    python3 -m unittest discover -s tests/unit

They need Python 3.11+ and `cryptography`; the NS8 `agent` package is
replaced by `tests/unit/fakeagent`.

## Container test

The rendered configuration is tested with the real runtime image, without
NethServer: three Docker network namespaces with fixed addresses stand for
the cluster VPN addresses.

    docker build -f server/Containerfile -t pg19c-server:test server
    python3 tests/container/cluster_test.py

It covers bootstrap, grants (SCRAM verifier, sharing between instances of
one application, revoke, drop), learner joins, replicas, a killed primary
with the endpoints following, `pg_rewind` rejoin, switchover, the dump
filter and a member leaving.

## Robot Framework

    run-ns8-tests <NODE_ADDR> ghcr.io/geniusdynamics/postgresql19c:<tag>

(the runner of `ns8-github-actions`). Suites:

- `10_standalone.robot`: install, a consumer database, rotation,
  isolation, backup dump, revoke, restart
- `20_cluster.robot`: four instances on the test node
  (`allow_same_node`): standalone to cluster, approval and join, witness,
  endpoints, failover, rejoin, rolling restart, synchronous mode, dump on
  the primary only, a member leaving, removal
- `30_openbao.robot`: a fake OpenBao (`tests/fake_openbao.py`), restart
  while sealed, turning off with and without copying

Dry run:

    python3 -m robot --dryrun -v NODE_ADDR:x -v IMAGE_URL:x tests/

## CI

`Publish images` builds both images (module and runtime) and publishes
them with the branch tag. `Test module on QEMU` runs the Robot suites on a
throwaway NS8 node. GitHub runs `workflow_run` workflows from the default
branch: until this branch is merged into `main`, the test workflow of
`main` (template values, `image_name: kickstart`) is the one that runs.
