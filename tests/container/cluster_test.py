#!/usr/bin/env python3

#
# Copyright (C) 2026 Genius Dynamics
# SPDX-License-Identifier: GPL-3.0-or-later
#

"""Container test of the runtime image and the rendered configuration,
without NethServer: three "nodes" (Docker network namespaces with fixed
addresses that stand for the cluster VPN addresses), each running the
etcd, Patroni and HAProxy containers exactly as the module renders them.

It checks the bootstrap of the first member, the learner join of etcd
members, replicas built by Patroni, the client endpoint following the
primary across a failover (the old primary rejoins with pg_rewind), a
switchover, client grants with a SCRAM verifier, the dump filter, and a
member leaving.

Usage (needs Docker and the image built from server/Containerfile):

    docker build -f server/Containerfile -t pg19c-server:test server
    python3 tests/container/cluster_test.py [--image pg19c-server:test] [--keep]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "imageroot", "pypkg"))

import pgha  # noqa: E402

NET = "pg19ctest"
SUBNET = "172.30.77.0/24"
PORTS = {"pg": 5432, "patroni": 8008, "etcd_client": 2379, "etcd_peer": 2380,
         "endpoint": 5000, "endpoint_ro": 5001, "pgadmin": 0}
HAPROXY_IMAGE = "docker.io/library/haproxy:3.2.27-alpine"


def sh(*cmd, check=True, input_text=None, timeout=300):
    proc = subprocess.run(cmd, text=True, capture_output=True, input=input_text, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:6])}... failed ({proc.returncode}): {proc.stderr[-1500:]}")
    return proc


def step(msg):
    print(f"\n=== {msg}", flush=True)


def wait(fn, timeout=180, every=2, what="condition"):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            value = fn()
            if value:
                return value
        except Exception as ex:  # noqa: BLE001
            last = ex
        time.sleep(every)
    raise RuntimeError(f"Timeout waiting for {what}: {last}")


class Node:
    def __init__(self, test, index):
        self.test = test
        self.name = f"pgnode{index}"
        self.ip = f"172.30.77.{10 + index}"
        self.dir = os.path.join(test.tmp, self.name)
        os.makedirs(self.dir)

    @property
    def c(self):
        return {k: f"{self.name}-{k}" for k in ("net", "etcd", "patroni", "pgproxy")}

    def member(self):
        return {"module_id": self.name, "host": self.ip, "pg_port": PORTS["pg"],
                "patroni_port": PORTS["patroni"], "etcd_client_port": PORTS["etcd_client"],
                "etcd_peer_port": PORTS["etcd_peer"], "role": "join"}

    def write_tls(self, ca_cert, ca_key):
        cert, key = pgha.make_node_cert(ca_cert, ca_key, self.name, {"127.0.0.1", self.ip})
        for comp in ("etcd", "patroni"):
            d = os.path.join(self.dir, comp, "tls")
            os.makedirs(d, exist_ok=True)
            for fname, text in (("ca.crt", ca_cert), ("node.crt", cert), ("node.key", key)):
                with open(os.path.join(d, fname), "w") as fp:
                    fp.write(text)

    def write(self, comp, fname, text):
        os.makedirs(os.path.join(self.dir, comp), exist_ok=True)
        with open(os.path.join(self.dir, comp, fname), "w") as fp:
            fp.write(text)

    def start_net(self):
        sh("docker", "run", "-d", "--name", self.c["net"], "--network", NET, "--ip", self.ip,
           "--entrypoint", "sleep", self.test.image, "infinity")

    def start_etcd(self, initial_cluster="", state="new"):
        self.write("etcd", "etcd.env", pgha.render_etcd_env({
            "module_id": self.name, "host": self.ip, "ports": PORTS, "initial_cluster": initial_cluster,
            "initial_state": state, "token": "pgnode1"}))
        sh("docker", "rm", "-f", self.c["etcd"], check=False)
        sh("docker", "run", "-d", "--name", self.c["etcd"], "--network", f"container:{self.c['net']}",
           "--entrypoint", "/usr/local/bin/ns8-etcd-start",
           "--env-file", os.path.join(self.dir, "etcd", "etcd.env"),
           "-v", f"{self.name}-etcd-data:/var/lib/etcd", "-v", f"{os.path.join(self.dir, 'etcd')}:/etc/etcd",
           self.test.image)

    def start_patroni(self, members):
        hosts = [f"127.0.0.1:{PORTS['etcd_client']}"] + [
            f"{m['host']}:{m['etcd_client_port']}" for m in members if m["module_id"] != self.name]
        self.write("patroni", "patroni.yml", pgha.render_patroni({
            "module_id": self.name, "scope": "pgnode1", "host": self.ip, "ports": PORTS,
            "secrets": self.test.secrets, "etcd_hosts": hosts, "max_connections": 300,
            "replication_mode": "async"}))
        sh("docker", "rm", "-f", self.c["patroni"], check=False)
        sh("docker", "run", "-d", "--name", self.c["patroni"], "--network", f"container:{self.c['net']}",
           "--entrypoint", "/usr/local/bin/ns8-patroni-start",
           "-v", f"{self.name}-pgdata:/var/lib/postgresql", "-v", f"{os.path.join(self.dir, 'patroni')}:/etc/patroni",
           self.test.image)

    def start_proxy(self, members):
        self.write("pgproxy", "haproxy.cfg", pgha.render_haproxy({
            "host": self.ip, "ports": PORTS, "members": members}))
        if sh("docker", "inspect", self.c["pgproxy"], check=False).returncode == 0:
            sh("docker", "kill", "-s", "USR2", self.c["pgproxy"])
            return
        sh("docker", "run", "-d", "--name", self.c["pgproxy"], "--network", f"container:{self.c['net']}",
           "-v", f"{os.path.join(self.dir, 'pgproxy')}:/usr/local/etc/haproxy:ro", HAPROXY_IMAGE)

    def etcdctl(self, *args, endpoints=None):
        eps = endpoints or f"https://127.0.0.1:{PORTS['etcd_client']}"
        return sh("docker", "exec", self.c["etcd"], "etcdctl", f"--endpoints={eps}", *args, check=False)

    def rest(self, path):
        proc = sh("docker", "exec", self.c["net"], "python3", "-c",
                  "import urllib.request,sys\n"
                  "try:\n r=urllib.request.urlopen(sys.argv[1],timeout=3); print(r.status); print(r.read().decode())\n"
                  "except urllib.error.HTTPError as e: print(e.code); print(e.read().decode())\n",
                  f"http://{self.ip}:{PORTS['patroni']}{path}", check=False)
        lines = proc.stdout.splitlines()
        return (int(lines[0]) if lines else 0), "\n".join(lines[1:])

    def sql(self, sql, port=None, user="postgres", password=None, db="postgres"):
        password = password or self.test.secrets["PG_SUPERUSER_PASSWORD"]
        # The password goes through a pgpass file of the client container
        pgpass = os.path.join(self.dir, f"pgpass-{user}")
        with open(pgpass, "w") as fp:
            fp.write(f"*:*:*:{user}:{password}\n")
        os.chmod(pgpass, 0o600)
        return sh("docker", "run", "--rm", "-i", "--network", f"container:{self.c['net']}",
                  "--entrypoint", "psql", "-v", f"{pgpass}:/pgpass:ro", "-e", "PGPASSFILE=/pgpass",
                  "-e", "PGCONNECT_TIMEOUT=5", self.test.image,
                  "-X", "-q", "-A", "-t", "-h", "127.0.0.1", "-p", str(port or PORTS["endpoint"]),
                  "-U", user, "-d", db, "-v", "ON_ERROR_STOP=1", input_text=sql, check=False)


class ClusterTest:
    def __init__(self, image, keep):
        self.image = image
        self.keep = keep
        self.tmp = tempfile.mkdtemp(prefix="pg19ctest-")
        self.nodes = [Node(self, i) for i in (1, 2, 3)]
        self.secrets = {k: pgha.new_password() for k in pgha.SECRET_KEYS}
        self.ca_cert, self.ca_key = pgha.make_ca("test CA")

    def cleanup(self):
        for n in self.nodes:
            for c in n.c.values():
                sh("docker", "rm", "-f", c, check=False)
            sh("docker", "volume", "rm", "-f", f"{n.name}-pgdata", f"{n.name}-etcd-data", check=False)
        sh("docker", "network", "rm", NET, check=False)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def leader(self, via):
        status, body = via.rest("/cluster")
        if status != 200:
            return None
        for m in json.loads(body).get("members", []):
            if m["role"] == "leader" and m["state"] == "running":
                return m["name"]
        return None

    def streaming(self, via, count):
        status, body = via.rest("/cluster")
        if status != 200:
            return False
        ms = json.loads(body).get("members", [])
        return len([m for m in ms if m["role"] != "leader" and m["state"] == "streaming"]) >= count

    def node(self, name):
        return next(n for n in self.nodes if n.name == name)

    def join(self, node, existing):
        """The admit-member flow: learner add on an existing member, start,
        promote, then Patroni builds the replica"""
        peer = pgha.etcd_url(node.ip, PORTS["etcd_peer"])
        proc = existing.etcdctl("member", "add", node.name, f"--peer-urls={peer}", "--learner", "-w", "json")
        assert proc.returncode == 0, f"member add: {proc.stdout} {proc.stderr}"
        initial = pgha.etcd_initial_cluster(json.loads(proc.stdout)["members"], node.name, peer)
        node.write_tls(self.ca_cert, self.ca_key)
        node.start_net()
        node.start_etcd(initial, "existing")
        eps = f"https://{existing.ip}:{PORTS['etcd_client']}"

        def promote():
            members = json.loads(existing.etcdctl("member", "list", "-w", "json").stdout)["members"]
            mine = [m for m in members if m.get("name") == node.name]
            if mine and not mine[0].get("isLearner"):
                return True
            if mine:
                existing.etcdctl("member", "promote", format(int(mine[0]["ID"]), "x"), endpoints=eps)
            return False
        wait(promote, 120, 3, f"promotion of {node.name}")

    def run(self):
        sh("docker", "network", "create", "--subnet", SUBNET, NET)
        n1, n2, n3 = self.nodes
        members = [n.member() for n in self.nodes]

        step("Bootstrap the first member (standalone)")
        n1.write_tls(self.ca_cert, self.ca_key)
        n1.start_net()
        n1.start_etcd()
        n1.start_patroni([n1.member()])
        wait(lambda: self.leader(n1) == n1.name, 180, 3, "first leader")
        n1.start_proxy([n1.member()])
        out = wait(lambda: n1.sql("SELECT version();").stdout, 60, 2, "endpoint")
        assert "PostgreSQL 19" in out, out
        print(out.strip())
        assert n1.sql("SHOW max_locks_per_transaction;").stdout.strip() == "128"
        assert n1.sql("SHOW password_encryption;").stdout.strip() == "scram-sha-256"
        assert n1.sql("SHOW wal_sender_shutdown_timeout;").stdout.strip() == "30s"
        n1.sql("CREATE TABLE t (id serial primary key, v text); INSERT INTO t (v) VALUES ('standalone');")

        step("Grant a client database with a SCRAM verifier")
        pw = pgha.new_password()
        proc = n1.sql(pgha.grant_sql("warpgate1", "warpgate1", pgha.scram_sha256_verifier(pw)))
        assert proc.returncode == 0, proc.stderr
        proc = n1.sql("CREATE TABLE x (a int); INSERT INTO x VALUES (1); SELECT count(*) FROM x;",
                      user="warpgate1", password=pw, db="warpgate1")
        assert proc.stdout.strip() == "1", proc.stderr
        assert n1.sql("SELECT 1;", user="warpgate1", password="wrong", db="warpgate1").returncode != 0
        assert n1.sql("SELECT 1;", user="warpgate1", password=pw, db="postgres").returncode != 0, \
            "the client must not connect to other databases"
        proc = n1.sql(pgha.grant_sql("warpgate1", "warpgate1", pgha.scram_sha256_verifier(pw)))
        assert proc.returncode == 0, "grant must be idempotent: " + proc.stderr
        out = n1.sql(pgha.LIST_DATABASES_SQL).stdout.strip()
        dbs = json.loads(out)
        assert dbs[0]["name"] == "warpgate1" and dbs[0]["client_id"] == "warpgate1", dbs

        step("Join two members as etcd learners, then Patroni replicas")
        for node in (n2, n3):
            self.join(node, n1)
            node.start_patroni(members)
        wait(lambda: self.streaming(n1, 2), 300, 3, "two streaming replicas")
        out = n1.etcdctl("member", "list", "-w", "json").stdout
        assert len(json.loads(out)["members"]) == 3
        for n in self.nodes:
            n.start_proxy(members)
        time.sleep(4)
        for n in self.nodes:
            out = wait(lambda n=n: n.sql("SELECT v FROM t ORDER BY id LIMIT 1;").stdout.strip(), 60, 2,
                       f"endpoint of {n.name}")
            assert out == "standalone", out
            assert n.sql("SELECT pg_is_in_recovery();").stdout.strip() == "f"
        out = n3.sql("SELECT pg_is_in_recovery();", port=PORTS["endpoint_ro"]).stdout.strip()
        assert out in ("t", "f"), out

        step("Failover: kill the primary, a replica is promoted, the endpoints follow")
        n1.sql("INSERT INTO t (v) VALUES ('before-failover');")
        old = self.leader(n2)
        sh("docker", "kill", self.node(old).c["patroni"])
        survivor = n3 if old != n3.name else n2
        t0 = time.time()
        new = wait(lambda: (lambda l: l if l and l != old else None)(self.leader(survivor)), 120, 2, "new leader")
        print(f"primary {old} -> {new} in {time.time() - t0:.0f}s")
        for n in self.nodes:
            if n.name == old:
                continue
            out = wait(lambda n=n: n.sql("INSERT INTO t (v) VALUES ('after') RETURNING id;").stdout.strip(),
                       60, 2, f"write through {n.name}")
            assert out, out
        print(f"writes through the endpoints work {time.time() - t0:.0f}s after the kill")

        step("The old primary rejoins as a replica")
        oldn = self.node(old)
        oldn.start_patroni(members)
        wait(lambda: self.streaming(survivor, 2), 300, 3, "old primary streaming again")
        out = oldn.sql("SELECT count(*) FROM t WHERE v = 'after';").stdout.strip()
        assert int(out) >= 2, out

        step("Switchover back through the REST API")
        target = old
        proc = sh("docker", "exec", self.node(new).c["patroni"], "gosu", "postgres", "patronictl", "switchover",
                  "--leader", new, "--candidate", target, "--force", check=False)
        print(proc.stdout[-400:], proc.stderr[-400:])
        wait(lambda: self.leader(survivor) == target, 120, 2, "switchover")
        wait(lambda: n2.sql("SELECT pg_is_in_recovery();").stdout.strip() == "f", 60, 2, "endpoint after switchover")

        step("Dump on the primary, without the system roles")
        leader = self.node(self.leader(n2))
        proc = sh("docker", "exec", leader.c["patroni"], "gosu", "postgres", "pg_dumpall", "--clean", "--if-exists")
        filtered = "".join(pgha.filter_dump(proc.stdout.splitlines(keepends=True)))
        assert "CREATE ROLE warpgate1" in filtered
        for r in pgha.SYSTEM_ROLES:
            assert f"CREATE ROLE {r};" not in filtered, r
        print(f"dump: {len(filtered)} bytes")

        step("A member leaves: Patroni stops, the etcd member is removed")
        leaving = next(n for n in self.nodes if n.name != leader.name)
        sh("docker", "stop", leaving.c["patroni"])
        mid = [m for m in json.loads(leader.etcdctl("member", "list", "-w", "json").stdout)["members"]
               if m["name"] == leaving.name][0]["ID"]
        assert leader.etcdctl("member", "remove", format(int(mid), "x")).returncode == 0
        remaining = [n.member() for n in self.nodes if n is not leaving]
        for n in self.nodes:
            if n is not leaving:
                n.start_proxy(remaining)
        out = json.loads(leader.etcdctl("member", "list", "-w", "json").stdout)["members"]
        assert len(out) == 2
        assert leader.sql("SELECT count(*) FROM t;").returncode == 0
        print("\nALL CHECKS PASSED")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="pg19c-server:test")
    parser.add_argument("--keep", action="store_true", help="keep the containers for inspection")
    args = parser.parse_args()
    test = ClusterTest(args.image, args.keep)
    test.cleanup()
    test = ClusterTest(args.image, args.keep)
    try:
        test.run()
    except Exception:
        for n in test.nodes:
            for comp in ("etcd", "patroni", "pgproxy"):
                logs = sh("docker", "logs", "--tail", "25", n.c[comp], check=False)
                print(f"--- {n.c[comp]}\n{logs.stdout[-2500:]}{logs.stderr[-2500:]}")
        raise
    finally:
        if not args.keep:
            test.cleanup()


if __name__ == "__main__":
    main()
