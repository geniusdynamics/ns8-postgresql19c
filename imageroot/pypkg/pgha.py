#
# Copyright (C) 2026 Genius Dynamics
# SPDX-License-Identifier: GPL-3.0-or-later
#

"""Shared helpers of the ns8-postgresql19c actions, events and commands.

An instance runs, with host networking, the containers:

- etcd: one member of the consensus store (DCS) of Patroni
- patroni: Patroni, which starts PostgreSQL 19 as primary or replica
- pgproxy: HAProxy, the client endpoint that always reaches the primary
- pgadmin: optional pgAdmin behind Traefik

A standalone instance is a cluster of one member; a cluster is several
instances (one per node) whose etcd members form one store. See
docs/architecture.md.

Secrets (the PostgreSQL superuser, replication and rewind passwords, the
Patroni REST password, the cluster CA key) live in 0600 files under the
state directory. They never go to the module environment, the cluster
Redis, argv or logs.

The functions that render configuration are pure: they take plain values
and return text, so the unit tests exercise them without NS8.
"""

import base64
import contextlib
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IMAGE_NAME = "postgresql19c"
SERVICE = "postgresql19c"                   # consumer record
CLUSTER_SERVICE = "postgresql19c-cluster"   # member record
EVENT_CHANGED = "postgresql19c-changed"
EVENT_CLUSTER_CHANGED = "postgresql19c-cluster-changed"
ROLES = ("standalone", "initial", "join", "witness")
CLUSTER_ROLES = ("initial", "join", "witness")
DATA_ROLES = ("standalone", "initial", "join")   # roles running PostgreSQL
REPLICATION_MODES = ("async", "sync")
PG_MAJOR = "19"
PG_VERSION = "19.0"
DATA_DIR = "/var/lib/postgresql/19/docker"
BIN_DIR = "/usr/lib/postgresql/19/bin"
NAMESPACE = "/ns8-postgresql19c/"
SUPERUSER = "postgres"
REPLICATION_USER = "replicator"
REWIND_USER = "rewind_user"
REST_USER = "patroni"
SYSTEM_ROLES = (SUPERUSER, REPLICATION_USER, REWIND_USER)
DEFAULT_MAX_CONNECTIONS = 300
UNITS = {
    "etcd": "etcd.service",
    "patroni": "patroni.service",
    "pgproxy": "pgproxy.service",
    "pgadmin": "pgadmin.service",
}
CONTAINERS = {"etcd": "etcd", "patroni": "patroni", "pgproxy": "pgproxy", "pgadmin": "pgadmin"}
MODULE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*[0-9]+$")
CLIENT_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,62}$")
DB_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
RESERVED_NAMES = set(SYSTEM_ROLES) | {"template0", "template1", "public", "patroni", "pgadmin"}
PORT_NAMES = ("pg", "patroni", "etcd_client", "etcd_peer", "endpoint", "endpoint_ro", "pgadmin")
SECRET_KEYS = ("PG_SUPERUSER_PASSWORD", "PG_REPLICATION_PASSWORD", "PG_REWIND_PASSWORD",
               "PATRONI_REST_PASSWORD")
# Cluster secrets: shared by every member; the pgAdmin password is per instance
CLUSTER_SECRET_KEYS = SECRET_KEYS


def log(*args):
    print(*args, file=sys.stderr)


def _agent():
    import agent
    return agent


def warning(msg):
    log(_agent().SD_WARNING + msg)


def error(msg):
    log(_agent().SD_ERR + msg)


# ---------------------------------------------------------------------------
# State files and secrets
# ---------------------------------------------------------------------------

SECRETS_FILE = "secrets.env"
TLS_DIR = "tls"


def env(name, default=""):
    return os.environ.get(name, default)


def state_dir():
    return env("AGENT_STATE_DIR") or os.getcwd()


def state_path(*parts):
    return os.path.join(state_dir(), *parts)


def write_private(path, text, mode=0o600):
    """Atomic write of a file created with the given mode"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w") as fp:
            fp.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_if_changed(path, text, mode=0o600):
    """Write the file only when its content differs. Returns True if written."""
    try:
        with open(path) as fp:
            if fp.read() == text:
                os.chmod(path, mode)
                return False
    except FileNotFoundError:
        pass
    write_private(path, text, mode)
    return True


def parse_envfile(text):
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def read_envfile(path):
    try:
        with open(path) as fp:
            return parse_envfile(fp.read())
    except FileNotFoundError:
        return {}


def read_secrets():
    return read_envfile(state_path(SECRETS_FILE))


def write_secrets(values):
    text = "".join(f"{k}={v}\n" for k, v in sorted(values.items()))
    write_private(state_path(SECRETS_FILE), text)


def new_password():
    # Hexadecimal: safe in URLs, YAML, pgpass and SQL literals without escaping
    return secrets.token_hex(32)


def ensure_secrets(keys=SECRET_KEYS + ("PGADMIN_PASSWORD",)):
    values = read_secrets()
    changed = False
    for key in keys:
        if not values.get(key):
            values[key] = new_password()
            changed = True
    if changed:
        write_secrets(values)
    return values


def new_cluster_secrets():
    """Replace the cluster secrets (a member leaving its cluster starts a
    new one of its own); keeps the pgAdmin password"""
    values = read_secrets()
    for key in CLUSTER_SECRET_KEYS:
        values[key] = new_password()
    if not values.get("PGADMIN_PASSWORD"):
        values["PGADMIN_PASSWORD"] = new_password()
    write_secrets(values)
    return values


# ---------------------------------------------------------------------------
# Environment, roles and ports
# ---------------------------------------------------------------------------

def role():
    value = env("PG_ROLE")
    return value if value in ROLES else ""


def is_cluster(value=None):
    return (role() if value is None else value) in CLUSTER_ROLES


def runs_postgres(value=None):
    return (role() if value is None else value) in DATA_ROLES


def ha_mode(value=None):
    return "cluster" if is_cluster(value) else "standalone"


def cluster_id():
    return env("PG_CLUSTER_ID") or env("MODULE_ID")


def replication_mode():
    value = env("PG_REPLICATION_MODE", "async")
    return value if value in REPLICATION_MODES else "async"


def max_connections():
    try:
        return int(env("PG_MAX_CONNECTIONS", str(DEFAULT_MAX_CONNECTIONS)))
    except ValueError:
        return DEFAULT_MAX_CONNECTIONS


def parse_ports(tcp_ports, tcp_port=""):
    """{"pg": 20001, "patroni": ..., } from the module TCP_PORTS list"""
    values = [int(p) for p in (tcp_ports or "").split(",") if p.strip().isdigit()]
    if not values and str(tcp_port).isdigit():
        values = [int(tcp_port)]
    while len(values) < len(PORT_NAMES):
        values.append(0)
    return dict(zip(PORT_NAMES, values))


def ports():
    return parse_ports(env("TCP_PORTS"), env("TCP_PORT"))


_vpn_cache = {}


def vpn_ip(node_id=None, rdb=None):
    """VPN address of a node (the local one by default), "" if unknown"""
    node_id = str(node_id or env("NODE_ID"))
    if node_id in _vpn_cache:
        return _vpn_cache[node_id]
    value = ""
    try:
        rdb = rdb or _agent().redis_connect(use_replica=True)
        value = rdb.hget(f"node/{node_id}/vpn", "ip_address") or ""
        ipaddress.ip_address(value)
    except Exception as ex:
        if value:
            warning(f"Invalid VPN address of node {node_id}: {ex}")
        value = ""
    _vpn_cache[node_id] = value
    return value


def local_host():
    """Address the instance listens on and announces to its peers"""
    return env("PG_HOST_OVERRIDE") or vpn_ip() or "127.0.0.1"


def server_image():
    return env("POSTGRESQL19C_SERVER_IMAGE")


def member_sort_key(module_id):
    digits = re.findall(r"\d+$", module_id)
    return (int(digits[0]) if digits else 0, module_id)


# ---------------------------------------------------------------------------
# TLS: a CA per cluster, a certificate per member (etcd client and peer
# mutual TLS; Patroni authenticates to etcd with it)
# ---------------------------------------------------------------------------

def tls_path(name):
    return state_path(TLS_DIR, name)


def read_text(path):
    with open(path) as fp:
        return fp.read()


def make_ca(common_name):
    """Return (cert_pem, key_pem) of a new CA"""
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=3650 * 3))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM).decode(),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()).decode())


def make_node_cert(ca_cert_pem, ca_key_pem, common_name, ips):
    """Return (cert_pem, key_pem) of a member certificate valid as TLS
    server and client for the given IP addresses"""
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem.encode())
    ca_key = serialization.load_pem_private_key(ca_key_pem.encode(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    sans = [x509.DNSName("localhost")]
    for ip in sorted(set(ips)):
        sans.append(x509.IPAddress(ipaddress.ip_address(ip)))
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH,
                                                  ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, key_cert_sign=False,
                                         crl_sign=False, content_commitment=False, data_encipherment=False,
                                         key_agreement=True, encipher_only=False, decipher_only=False),
                           critical=True)
            # Python 3.13 (Patroni) verifies in strict X.509 mode: key identifiers are required
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                           critical=False)
            .sign(ca_key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM).decode(),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()).decode())


def cert_ips(cert_pem):
    from cryptography import x509
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return set()
    return {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}


def cert_issued_by(cert_pem, ca_cert_pem):
    from cryptography import x509
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    ca = x509.load_pem_x509_certificate(ca_cert_pem.encode())
    if cert.issuer != ca.subject:
        return False
    try:
        cert.verify_directly_issued_by(ca)
        return True
    except Exception:
        return False


def store_ca(cert_pem, key_pem):
    os.makedirs(state_path(TLS_DIR), mode=0o700, exist_ok=True)
    write_private(tls_path("ca.crt"), cert_pem)
    write_private(tls_path("ca.key"), key_pem)
    for name in ("node.crt", "node.key"):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tls_path(name))


def ensure_ca():
    """Create the cluster CA if missing. Returns (cert_pem, key_pem)."""
    if os.path.exists(tls_path("ca.crt")) and os.path.exists(tls_path("ca.key")):
        return read_text(tls_path("ca.crt")), read_text(tls_path("ca.key"))
    cert, key = make_ca(f"ns8-postgresql19c CA {env('MODULE_ID')}")
    store_ca(cert, key)
    return cert, key


def ensure_node_cert(host):
    """Member certificate for 127.0.0.1 and the announced address,
    re-issued when the address or the CA changed"""
    ca_cert, ca_key = ensure_ca()
    wanted = {"127.0.0.1", host}
    try:
        cert = read_text(tls_path("node.crt"))
        read_text(tls_path("node.key"))
        if wanted <= cert_ips(cert) and cert_issued_by(cert, ca_cert):
            return False
    except FileNotFoundError:
        pass
    cert, key = make_node_cert(ca_cert, ca_key, env("MODULE_ID") or "member", wanted)
    write_private(tls_path("node.crt"), cert)
    write_private(tls_path("node.key"), key)
    return True


# ---------------------------------------------------------------------------
# Rendering (pure functions)
# ---------------------------------------------------------------------------

def etcd_url(host, port):
    return f"https://{host}:{port}"


def endpoint(host, port):
    return f"{host}:{port}"


def pg_hba():
    """SCRAM only (md5 is deprecated in PostgreSQL 19). The local socket is
    reachable only inside the Patroni container (podman exec)."""
    return [
        "local all all peer",
        f"host replication {REPLICATION_USER} 127.0.0.1/32 scram-sha-256",
        f"host replication {REPLICATION_USER} 0.0.0.0/0 scram-sha-256",
        "host all all 127.0.0.1/32 scram-sha-256",
        "host all all 0.0.0.0/0 scram-sha-256",
    ]


def dcs_parameters(max_conn=DEFAULT_MAX_CONNECTIONS):
    """Cluster-wide PostgreSQL parameters, kept in the DCS by Patroni.
    Parameters that must be equal on every member are explicit, so that
    Patroni's own defaults (max_locks_per_transaction 64) do not differ
    from the PostgreSQL 19 defaults (128)."""
    return {
        "max_connections": int(max_conn),
        "max_locks_per_transaction": 128,
        "max_prepared_transactions": 0,
        "max_worker_processes": 8,
        "max_wal_senders": 10,
        "max_replication_slots": 10,
        "wal_level": "replica",
        "wal_log_hints": "on",
        "hot_standby": "on",
        "track_commit_timestamp": "off",
        "wal_keep_size": "256MB",
        "password_encryption": "scram-sha-256",
        # PostgreSQL 19: a replica that stops acknowledging cannot block
        # the shutdown of the primary (switchover, restart) forever
        "wal_sender_shutdown_timeout": "30s",
    }


def render_patroni(cfg):
    """patroni.yml as JSON (YAML is a superset of JSON). cfg keys:
    module_id, scope, host, ports, secrets, etcd_hosts, max_connections,
    replication_mode"""
    host = cfg["host"]
    p = cfg["ports"]
    s = cfg["secrets"]
    listen = "127.0.0.1" if host == "127.0.0.1" else f"127.0.0.1,{host}"
    sync = cfg.get("replication_mode") == "sync"
    doc = {
        "scope": cfg["scope"],
        "namespace": NAMESPACE,
        "name": cfg["module_id"],
        "log": {"level": "INFO", "format": "%(levelname)s: %(message)s"},
        "restapi": {
            "listen": endpoint(host, p["patroni"]),
            "connect_address": endpoint(host, p["patroni"]),
            "authentication": {"username": REST_USER, "password": s["PATRONI_REST_PASSWORD"]},
        },
        "ctl": {"insecure": False},
        "etcd3": {
            "hosts": cfg["etcd_hosts"],
            "protocol": "https",
            "cacert": "/run/patroni/ca.crt",
            "cert": "/run/patroni/node.crt",
            "key": "/run/patroni/node.key",
        },
        "bootstrap": {
            "dcs": {
                "ttl": 30,
                "loop_wait": 10,
                "retry_timeout": 10,
                "maximum_lag_on_failover": 1048576,
                # The primary keeps running while the DCS is down, as long as
                # every member confirms it through the REST API: never two
                # primaries, and no outage when only etcd fails
                "failsafe_mode": True,
                "synchronous_mode": sync,
                "synchronous_mode_strict": False,
                "postgresql": {
                    "use_pg_rewind": True,
                    "use_slots": True,
                    "parameters": dcs_parameters(cfg.get("max_connections", DEFAULT_MAX_CONNECTIONS)),
                },
            },
            "initdb": [{"encoding": "UTF8"}, {"auth-local": "peer"}, {"auth-host": "scram-sha-256"}],
        },
        "postgresql": {
            "listen": f"{listen}:{p['pg']}",
            "connect_address": endpoint(host, p["pg"]),
            "data_dir": DATA_DIR,
            "bin_dir": BIN_DIR,
            "pgpass": "/run/patroni/pgpass.patroni",
            "authentication": {
                "superuser": {"username": SUPERUSER, "password": s["PG_SUPERUSER_PASSWORD"]},
                "replication": {"username": REPLICATION_USER, "password": s["PG_REPLICATION_PASSWORD"]},
                "rewind": {"username": REWIND_USER, "password": s["PG_REWIND_PASSWORD"]},
            },
            "parameters": {
                "unix_socket_directories": "/var/run/postgresql",
                "password_encryption": "scram-sha-256",
            },
            "pg_hba": pg_hba(),
            "create_replica_methods": ["basebackup"],
            "basebackup": [{"checkpoint": "fast"}],
        },
        "tags": {"nofailover": False, "noloadbalance": False, "clonefrom": False, "nosync": False},
    }
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def render_etcd_env(cfg):
    """Environment file of the etcd container. cfg keys: module_id, host,
    ports, initial_cluster, initial_state, token"""
    host = cfg["host"]
    p = cfg["ports"]
    client_urls = [etcd_url("127.0.0.1", p["etcd_client"])]
    if host != "127.0.0.1":
        client_urls.append(etcd_url(host, p["etcd_client"]))
    peer = etcd_url(host, p["etcd_peer"])
    initial = cfg.get("initial_cluster") or f"{cfg['module_id']}={peer}"
    values = {
        "ETCD_NAME": cfg["module_id"],
        "ETCD_DATA_DIR": "/var/lib/etcd",
        "ETCD_LISTEN_CLIENT_URLS": ",".join(client_urls),
        "ETCD_ADVERTISE_CLIENT_URLS": etcd_url(host, p["etcd_client"]),
        "ETCD_LISTEN_PEER_URLS": peer,
        "ETCD_INITIAL_ADVERTISE_PEER_URLS": peer,
        "ETCD_INITIAL_CLUSTER": initial,
        "ETCD_INITIAL_CLUSTER_STATE": cfg.get("initial_state") or "new",
        "ETCD_INITIAL_CLUSTER_TOKEN": cfg.get("token") or cfg["module_id"],
        "ETCD_CLIENT_CERT_AUTH": "true",
        "ETCD_TRUSTED_CA_FILE": "/etc/etcd/tls/ca.crt",
        "ETCD_CERT_FILE": "/etc/etcd/tls/node.crt",
        "ETCD_KEY_FILE": "/etc/etcd/tls/node.key",
        "ETCD_PEER_CLIENT_CERT_AUTH": "true",
        "ETCD_PEER_TRUSTED_CA_FILE": "/etc/etcd/tls/ca.crt",
        "ETCD_PEER_CERT_FILE": "/etc/etcd/tls/node.crt",
        "ETCD_PEER_KEY_FILE": "/etc/etcd/tls/node.key",
        # Cluster VPN between nodes: tolerate more latency than a LAN
        "ETCD_HEARTBEAT_INTERVAL": "250",
        "ETCD_ELECTION_TIMEOUT": "2500",
        "ETCD_AUTO_COMPACTION_MODE": "periodic",
        "ETCD_AUTO_COMPACTION_RETENTION": "1h",
        "ETCD_QUOTA_BACKEND_BYTES": str(512 * 1024 * 1024),
        "ETCD_LOG_LEVEL": "warn",
        "ETCDCTL_CACERT": "/etc/etcd/tls/ca.crt",
        "ETCDCTL_CERT": "/etc/etcd/tls/node.crt",
        "ETCDCTL_KEY": "/etc/etcd/tls/node.key",
    }
    return "".join(f"{k}={v}\n" for k, v in values.items())


def render_haproxy(cfg):
    """haproxy.cfg of the client endpoint. cfg keys: host, ports, members
    ([{module_id, host, pg_port, patroni_port}] of the members that run
    PostgreSQL)"""
    host = cfg["host"]
    p = cfg["ports"]
    binds = ["127.0.0.1"] + ([host] if host != "127.0.0.1" else [])
    lines = [
        "# Rendered by ns8-postgresql19c: do not edit",
        "global",
        "    maxconn 4000",
        "    log stdout format raw local0 notice",
        "",
        "defaults",
        "    log global",
        "    mode tcp",
        "    option dontlognull",
        "    retries 2",
        "    timeout connect 4s",
        "    timeout client 60m",
        "    timeout server 60m",
        "    timeout check 3s",
        "",
    ]

    def section(name, port, uri, balance):
        out = [f"listen {name}"]
        out += [f"    bind {b}:{port}" for b in binds]
        out += [
            f"    balance {balance}",
            "    option httpchk",
            f"    http-check send meth GET uri {uri}",
            "    http-check expect status 200",
            "    default-server inter 2s fastinter 1s downinter 3s rise 2 fall 3 on-marked-down shutdown-sessions",
        ]
        for m in sorted(cfg["members"], key=lambda m: member_sort_key(m["module_id"])):
            out.append(f"    server {m['module_id']} {m['host']}:{m['pg_port']} check port {m['patroni_port']}")
        return out + [""]

    lines += section("primary", p["endpoint"], "/primary", "first")
    # Read-only endpoint: any running member (replicas and the primary) whose
    # replication lag is under 16 MB
    lines += section("readonly", p["endpoint_ro"], "/read-only?lag=16MB", "leastconn")
    return "\n".join(lines)


def render_pgpass(password, port=None):
    return f"*:{port or '*'}:*:{SUPERUSER}:{password}\n"


def render_pgadmin_servers(cfg):
    """servers.json of pgAdmin: the local read-write endpoint"""
    return json.dumps({"Servers": {"1": {
        "Name": f"PostgreSQL {PG_MAJOR} ({cfg['module_id']})",
        "Group": "NethServer",
        "Host": "127.0.0.1",
        "Port": int(cfg["ports"]["endpoint"]),
        "MaintenanceDB": "postgres",
        "Username": SUPERUSER,
        "SSLMode": "disable",
    }}}, indent=2) + "\n"


def initial_cluster_string(members):
    """name=peer_url list for ETCD_INITIAL_CLUSTER"""
    return ",".join(f"{name}={url}" for name, url in members)


def etcd_initial_cluster(member_list, new_name, new_peer_url):
    """ETCD_INITIAL_CLUSTER of a new member, from the JSON member list
    returned by etcd after "member add". Unstarted members have no name:
    the new member is recognised by its peer URL."""
    pairs = []
    for m in member_list:
        urls = m.get("peerURLs") or []
        name = m.get("name") or (new_name if new_peer_url in urls else "")
        if not name or not urls:
            continue
        pairs.append((name, urls[0]))
    if not any(n == new_name for n, _ in pairs):
        pairs.append((new_name, new_peer_url))
    return initial_cluster_string(pairs)


# ---------------------------------------------------------------------------
# SCRAM-SHA-256 verifier: client passwords are sent to PostgreSQL already
# hashed, so the clear text never reaches its logs or catalogs
# ---------------------------------------------------------------------------

def scram_sha256_verifier(password, salt=None, iterations=4096):
    salt = salt if salt is not None else secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode()
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def quote_ident(name):
    return '"' + name.replace('"', '""') + '"'


def quote_literal(text):
    return "'" + text.replace("'", "''") + "'"


def database_name_for(client_id):
    """Default database (and owner role) name of a client"""
    name = re.sub(r"[^a-z0-9_]", "_", client_id.lower())
    if not name or not name[0].isalpha():
        name = "db_" + name
    return name[:63]


def valid_database_name(name):
    return bool(DB_NAME_RE.match(name or "")) and name not in RESERVED_NAMES and not name.startswith("pg_")


def grant_sql(database, client_id, verifier):
    """SQL run on the primary (as superuser) to create or update the owner
    role and the database of a client. The role and database names are
    validated identifiers; the comment records the owning client."""
    role_q = quote_ident(database)
    comment = quote_literal(f"ns8-client={client_id}")
    return "\n".join([
        "\\set ON_ERROR_STOP on",
        # Clients connect to their own database only
        "REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;",
        "REVOKE CONNECT ON DATABASE template1 FROM PUBLIC;",
        f"GRANT CONNECT ON DATABASE postgres TO {REWIND_USER};",
        f"SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {quote_literal(database)}) AS role_exists \\gset",
        "\\if :role_exists",
        f"ALTER ROLE {role_q} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {quote_literal(verifier)};",
        "\\else",
        f"CREATE ROLE {role_q} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {quote_literal(verifier)};",
        "\\endif",
        f"COMMENT ON ROLE {role_q} IS {comment};",
        f"SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = {quote_literal(database)}) AS db_exists \\gset",
        "\\if :db_exists",
        f"ALTER DATABASE {role_q} OWNER TO {role_q};",
        "\\else",
        f"CREATE DATABASE {role_q} OWNER {role_q} ENCODING 'UTF8' TEMPLATE template0;",
        "\\endif",
        f"COMMENT ON DATABASE {role_q} IS {comment};",
        f"REVOKE ALL ON DATABASE {role_q} FROM PUBLIC;",
        f"GRANT ALL ON DATABASE {role_q} TO {role_q};",
        f"\\c {role_q}",
        f"ALTER SCHEMA public OWNER TO {role_q};",
        "",
    ])


def revoke_sql(database, drop=False):
    role_q = quote_ident(database)
    if drop:
        return "\n".join([
            "\\set ON_ERROR_STOP on",
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = {quote_literal(database)};",
            f"DROP DATABASE IF EXISTS {role_q} WITH (FORCE);",
            f"DROP ROLE IF EXISTS {role_q};",
            "",
        ])
    return "\n".join([
        "\\set ON_ERROR_STOP on",
        f"ALTER ROLE {role_q} WITH NOLOGIN PASSWORD NULL;",
        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = {quote_literal(database)};",
        "",
    ])


LIST_DATABASES_SQL = """
SELECT json_agg(row_to_json(t) ORDER BY t.name) FROM (
  SELECT d.datname AS name,
         pg_get_userbyid(d.datdba) AS owner,
         pg_database_size(d.datname) AS size_bytes,
         COALESCE(substring(shobj_description(d.oid, 'pg_database') FROM '^ns8-client=(.*)$'), '') AS client_id,
         COALESCE((SELECT r.rolcanlogin FROM pg_roles r WHERE r.oid = d.datdba), false) AS owner_can_login
  FROM pg_database d
  WHERE NOT d.datistemplate AND d.datname <> 'postgres'
) t;
"""


def filter_dump(text_lines):
    """Drop the statements of the system roles from a pg_dumpall output:
    Patroni creates them with the passwords of the restoring cluster."""
    pattern = re.compile(r'^(CREATE|ALTER|DROP) ROLE (IF EXISTS )?"?(%s)"?( |;)' % "|".join(SYSTEM_ROLES))
    for line in text_lines:
        if pattern.match(line):
            continue
        yield line


REPLICA_DUMP_MARKER = "-- ns8-postgresql19c: no dump on this member"


# ---------------------------------------------------------------------------
# Quorum (pure)
# ---------------------------------------------------------------------------

def quorum_summary(voters, healthy_voters, learners=0, pg_members=0):
    """Quorum facts of an etcd store of `voters` voting members"""
    voters = max(int(voters), 0)
    needed = voters // 2 + 1 if voters else 0
    tolerance = (voters - 1) // 2 if voters else 0
    warnings = []
    if 0 < voters < 3:
        warnings.append("below_quorum")
    if voters and voters % 2 == 0:
        warnings.append("even_voters")
    if healthy_voters < needed:
        warnings.append("quorum_lost")
    if learners:
        warnings.append("learner_pending")
    if pg_members == 1 and voters > 1:
        warnings.append("single_postgres_member")
    return {
        "voters": voters,
        "healthy_voters": int(healthy_voters),
        "quorum_size": needed,
        "quorum_ok": voters > 0 and healthy_voters >= needed,
        "failure_tolerance": tolerance,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Validation and callers
# ---------------------------------------------------------------------------

def validation_error(field, value, err, exit_code=3):
    agent = _agent()
    agent.set_status("validation-failed")
    json.dump([{"field": field, "parameter": field, "value": value, "error": err}], fp=sys.stdout)
    sys.exit(exit_code)


def caller_module():
    """Module ID of the caller, "" for administrators"""
    user = env("AGENT_TASK_USER")
    return user.removeprefix("module/") if user.startswith("module/") else ""


def resolve_client_id(data):
    caller = caller_module()
    requested = (data.get("client_id") or "").strip()
    if caller:
        if requested and requested != caller:
            validation_error("client_id", requested, "client_id_mismatch")
        return caller
    if not requested:
        validation_error("client_id", "", "required")
    if not CLIENT_ID_RE.match(requested):
        validation_error("client_id", requested, "invalid")
    return requested


# ---------------------------------------------------------------------------
# Podman helpers
# ---------------------------------------------------------------------------

def run(cmd, input_text=None, timeout=120, check=False):
    proc = subprocess.run(cmd, input=input_text, text=True, capture_output=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''} failed: {proc.stderr.strip()[-400:]}")
    return proc


def container_running(name):
    proc = run(["podman", "container", "inspect", "--format", "{{.State.Running}}", name], timeout=30)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def unit_active(name):
    return run(["systemctl", "--user", "-q", "is-active", name], timeout=30).returncode == 0


def systemctl(*args, check=True):
    proc = run(["systemctl", "--user", *args], timeout=900)
    if check and proc.returncode != 0:
        raise RuntimeError(f"systemctl {' '.join(args)} failed: {proc.stderr.strip()[-400:]}")
    return proc


def etcdctl(args, endpoints=None, timeout=60):
    """Run etcdctl with the member certificate. Uses the running etcd
    container, or a one-shot container when it is stopped."""
    eps = ",".join(endpoints) if endpoints else etcd_url("127.0.0.1", ports()["etcd_client"])
    base = ["etcdctl", f"--endpoints={eps}", "--dial-timeout=5s", "--command-timeout=20s"]
    if container_running(CONTAINERS["etcd"]):
        cmd = ["podman", "exec", CONTAINERS["etcd"]] + base + args
    else:
        render_etcd_dir_tls()
        cmd = ["podman", "run", "--rm", "--network=host", "--entrypoint=etcdctl",
               f"--volume={state_path('etcd')}:/etc/etcd:z",
               "--env=ETCDCTL_CACERT=/etc/etcd/tls/ca.crt",
               "--env=ETCDCTL_CERT=/etc/etcd/tls/node.crt",
               "--env=ETCDCTL_KEY=/etc/etcd/tls/node.key",
               server_image()] + base[1:] + args
    return run(cmd, timeout=timeout)


def etcd_json(args, endpoints=None):
    proc = etcdctl(args + ["-w", "json"], endpoints=endpoints)
    if proc.returncode != 0:
        raise RuntimeError(f"etcdctl {args[0]} failed: {proc.stderr.strip()[-300:]}")
    return json.loads(proc.stdout or "{}")


def etcd_members(endpoints=None):
    """[{id (hex), name, peer_urls, client_urls, learner}]"""
    data = etcd_json(["member", "list"], endpoints=endpoints)
    result = []
    for m in data.get("members", []):
        result.append({
            "id": format(int(m["ID"]), "x"),
            "name": m.get("name", ""),
            "peer_urls": m.get("peerURLs") or [],
            "client_urls": m.get("clientURLs") or [],
            "learner": bool(m.get("isLearner")),
        })
    return result


def psql(sql, database="postgres", timeout=120, tuples=True):
    """Run SQL on the primary through the local client endpoint, as the
    superuser. The password is read from a 0600 pgpass file, never from
    argv or the environment."""
    write_private(state_path("psql", "pgpass"), render_pgpass(read_secrets()["PG_SUPERUSER_PASSWORD"]))
    cmd = ["podman", "run", "--rm", "-i", "--network=host", "--entrypoint=psql",
           f"--volume={state_path('psql')}:/etc/psql:z", "--env=PGPASSFILE=/etc/psql/pgpass",
           "--env=PGCONNECT_TIMEOUT=5", server_image(),
           "-X", "-q", "-h", "127.0.0.1", "-p", str(ports()["endpoint"]), "-U", SUPERUSER,
           "-d", database, "-v", "ON_ERROR_STOP=1"] + (["-A", "-t"] if tuples else [])
    last = None
    for attempt in range(6):
        proc = run(cmd, input_text=sql, timeout=timeout)
        if proc.returncode == 0:
            return proc.stdout
        last = proc.stderr.strip()
        # The endpoint may have no primary for a few seconds (failover)
        if "could not connect" in last or "server closed" in last or "Connection refused" in last \
                or "read-only" in last or "the database system is" in last:
            time.sleep(5)
            continue
        break
    raise RuntimeError(f"psql failed: {(last or '')[-400:]}")


# ---------------------------------------------------------------------------
# Patroni REST API
# ---------------------------------------------------------------------------

def rest(host, port, path, method="GET", body=None, auth=False, timeout=5):
    """Returns (status, parsed JSON or text)"""
    url = f"http://{host}:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if auth:
        token = base64.b64encode(f"{REST_USER}:{read_secrets()['PATRONI_REST_PASSWORD']}".encode()).decode()
        req.add_header("Authorization", "Basic " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read().decode()
    except urllib.error.HTTPError as ex:
        status, raw = ex.code, ex.read().decode(errors="replace")
    try:
        return status, json.loads(raw) if raw else {}
    except ValueError:
        return status, raw


def local_patroni(path="/patroni", **kw):
    return rest(local_host(), ports()["patroni"], path, **kw)


def patroni_cluster(member_list=None):
    """GET /cluster from the local Patroni, or from any member"""
    candidates = []
    if runs_postgres():
        candidates.append((local_host(), ports()["patroni"]))
    for m in member_list or []:
        if m.get("patroni_port") and runs_postgres(m.get("role")):
            candidates.append((m["host"], int(m["patroni_port"])))
    for host, port in candidates:
        try:
            status, data = rest(host, port, "/cluster", timeout=3)
            if status == 200 and isinstance(data, dict):
                return data
        except Exception:
            continue
    return None


def is_local_primary():
    try:
        status, _ = local_patroni("/primary", timeout=3)
        return status == 200
    except Exception:
        return False


def wait_patroni(timeout=300, want_running=True):
    """Wait until the local Patroni answers and PostgreSQL runs"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, data = local_patroni("/patroni", timeout=3)
            if isinstance(data, dict) and (not want_running or data.get("state") == "running"):
                return data
        except Exception:
            pass
        time.sleep(3)
    return None


def wait_primary_endpoint(timeout=300):
    """Wait until SQL works through the local client endpoint"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if psql("SELECT 1;", timeout=30).strip() == "1":
                return True
        except Exception:
            pass
        time.sleep(3)
    return False


def apply_dynamic_config(mode=None, max_conn=None):
    """PATCH the cluster-wide settings kept by Patroni in the DCS. Returns
    True when something changed."""
    patch = {}
    try:
        status, current = local_patroni("/config", timeout=5)
    except Exception as ex:
        warning(f"Cannot read the Patroni configuration: {ex}")
        return False
    if status != 200 or not isinstance(current, dict):
        return False
    if mode is not None and bool(current.get("synchronous_mode")) != (mode == "sync"):
        patch["synchronous_mode"] = mode == "sync"
    params = (current.get("postgresql") or {}).get("parameters") or {}
    if max_conn is not None and str(params.get("max_connections")) != str(max_conn):
        patch.setdefault("postgresql", {}).setdefault("parameters", {})["max_connections"] = int(max_conn)
    if not patch:
        return False
    status, body = local_patroni("/config", method="PATCH", body=patch, auth=True)
    if status != 200:
        raise RuntimeError(f"Patroni rejected the configuration change ({status})")
    return True


def restart_pending():
    """Restart the members whose settings need it (max_connections),
    replicas first, through patronictl"""
    proc = run(["podman", "exec", "-u", "postgres", CONTAINERS["patroni"], "patronictl", "restart",
                cluster_id(), "--pending", "--force"], timeout=900)
    if proc.returncode != 0:
        warning(f"patronictl restart --pending failed: {proc.stderr.strip()[-300:]}")


def switchover_away(timeout=120):
    """If this member is the primary and a healthy replica exists, hand the
    primary role over gracefully. Returns True if no switchover was needed
    or it succeeded."""
    if not runs_postgres() or not is_local_primary():
        return True
    data = patroni_cluster() or {}
    candidates = [m for m in data.get("members", [])
                  if m.get("name") != env("MODULE_ID") and m.get("state") in ("streaming", "running")
                  and m.get("role") in ("replica", "sync_standby", "quorum_standby")
                  and not (m.get("tags") or {}).get("nofailover")]
    if not candidates:
        return False
    status, body = local_patroni("/switchover", method="POST",
                                 body={"leader": env("MODULE_ID")}, auth=True, timeout=timeout)
    if status != 200:
        warning(f"Switchover refused ({status}): {str(body)[:200]}")
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_local_primary():
            return True
        time.sleep(2)
    return False


# ---------------------------------------------------------------------------
# Discovery records and events
# ---------------------------------------------------------------------------

def cluster_records(rdb=None, wanted=None):
    """{module_id: record} of the postgresql19c-cluster service providers"""
    rdb = rdb or _agent().redis_connect(use_replica=True)
    result = {}
    for rec in _agent().list_service_providers(rdb, CLUSTER_SERVICE, "tcp"):
        if wanted is not None and rec.get("cluster_id") != wanted:
            continue
        result[rec["module_id"]] = rec
    return result


def record_to_member(module_id, rec):
    return {
        "module_id": module_id,
        "node": str(rec.get("node") or ""),
        "host": rec["host"],
        "role": rec.get("role", ""),
        "pg_port": int(rec.get("pg_port") or 0),
        "patroni_port": int(rec.get("patroni_port") or 0),
        "etcd_client_port": int(rec["etcd_client_port"]),
        "etcd_peer_port": int(rec["etcd_peer_port"]),
        "endpoint_port": int(rec.get("endpoint_port") or 0),
        "endpoint_ro_port": int(rec.get("endpoint_ro_port") or 0),
        "allow_same_node": rec.get("allow_same_node") == "true",
    }


def members(rdb=None, wanted=None):
    """Members of a cluster (records), ordered by module sequence number"""
    wanted = wanted if wanted is not None else cluster_id()
    result = []
    for module_id, rec in cluster_records(rdb, wanted).items():
        try:
            result.append(record_to_member(module_id, rec))
        except (KeyError, ValueError):
            warning(f"Ignoring the malformed {CLUSTER_SERVICE} record of {module_id}")
    result.sort(key=lambda m: member_sort_key(m["module_id"]))
    return result


def self_member():
    p = ports()
    return {
        "module_id": env("MODULE_ID"), "node": env("NODE_ID"), "host": local_host(), "role": role(),
        "pg_port": p["pg"] if runs_postgres() else 0,
        "patroni_port": p["patroni"] if runs_postgres() else 0,
        "etcd_client_port": p["etcd_client"], "etcd_peer_port": p["etcd_peer"],
        "endpoint_port": p["endpoint"], "endpoint_ro_port": p["endpoint_ro"],
        "allow_same_node": env("PG_ALLOW_SAME_NODE") == "1",
    }


MEMBERS_CACHE = "members.json"


def cluster_members(rdb=None):
    """Members of this instance's cluster, this one always included. The
    last list read from Redis is cached: units start from the cache when
    Redis is not reachable (e.g. while the whole NS8 cluster reboots)."""
    if not is_cluster():
        return [self_member()]
    try:
        others = [m for m in members(rdb) if m["module_id"] != env("MODULE_ID")]
        write_if_changed(state_path(MEMBERS_CACHE), json.dumps(others, sort_keys=True), 0o644)
    except Exception as ex:
        warning(f"Cluster records not readable, using the cached member list: {ex}")
        try:
            with open(state_path(MEMBERS_CACHE)) as fp:
                others = [m for m in json.load(fp) if m.get("module_id") != env("MODULE_ID")]
        except (FileNotFoundError, ValueError):
            others = []
    result = list(others)
    result.append(self_member())
    result.sort(key=lambda m: member_sort_key(m["module_id"]))
    return result


def cluster_candidates(rdb=None):
    """[{cluster_id, members, nodes}] of the clusters this instance can join"""
    clusters = {}
    for module_id, rec in cluster_records(rdb).items():
        cid = rec.get("cluster_id")
        if not cid or module_id == env("MODULE_ID"):
            continue
        entry = clusters.setdefault(cid, {"cluster_id": cid, "members": [], "nodes": []})
        entry["members"].append(module_id)
        if rec.get("node") and str(rec["node"]) not in entry["nodes"]:
            entry["nodes"].append(str(rec["node"]))
    for entry in clusters.values():
        entry["members"].sort(key=member_sort_key)
        entry["nodes"].sort()
    return sorted(clusters.values(), key=lambda c: c["cluster_id"])


def consumer_record():
    p = ports()
    value = role()
    return {
        "host": local_host(),
        "port": str(p["endpoint"]),
        "ro_port": str(p["endpoint_ro"]),
        "node": env("NODE_ID"),
        "ha_mode": ha_mode(value),
        "cluster": "true" if is_cluster(value) else "false",
        "cluster_id": cluster_id(),
        "role": value,
        "version": PG_VERSION,
        "tls": "false",
    }


def member_record():
    m = self_member()
    return {
        "host": m["host"], "node": m["node"], "cluster_id": cluster_id(), "role": m["role"],
        "pg_port": str(m["pg_port"]), "patroni_port": str(m["patroni_port"]),
        "etcd_client_port": str(m["etcd_client_port"]), "etcd_peer_port": str(m["etcd_peer_port"]),
        "endpoint_port": str(m["endpoint_port"]), "endpoint_ro_port": str(m["endpoint_ro_port"]),
        "allow_same_node": "true" if m["allow_same_node"] else "false",
    }


def publish_event(rdb, name, payload):
    rdb.publish(f"{env('AGENT_ID')}/event/{name}", json.dumps(payload))


def fire_cluster_changed(reason, rdb=None, cid=None, **extra):
    rdb = rdb or _agent().redis_connect(privileged=True)
    publish_event(rdb, EVENT_CLUSTER_CHANGED, {
        "cluster_id": cid or cluster_id(), "module_id": env("MODULE_ID"), "reason": reason, **extra,
    })


def publish_records(rdb=None, fire_events=True):
    """Publish (or remove) the service records of this instance. Returns
    True when the consumer record changed."""
    rdb = rdb or _agent().redis_connect(privileged=True)
    module_id = env("MODULE_ID")
    key = f"module/{module_id}/srv/tcp/{SERVICE}"
    ckey = f"module/{module_id}/srv/tcp/{CLUSTER_SERVICE}"
    current = rdb.hgetall(key)
    if not role():
        rdb.delete(key, ckey)
        return bool(current)
    record = consumer_record()
    if is_cluster():
        crec = member_record()
        if rdb.hgetall(ckey) != crec:
            trx = rdb.pipeline()
            trx.delete(ckey)
            trx.hset(ckey, mapping=crec)
            trx.execute()
    else:
        rdb.delete(ckey)
    changed = current != record
    if changed:
        trx = rdb.pipeline()
        trx.delete(key)
        trx.hset(key, mapping=record)
        trx.execute()
        if fire_events:
            publish_event(rdb, EVENT_CHANGED, {
                "module_id": module_id, "module_uuid": env("MODULE_UUID"),
                "cluster_id": cluster_id(), "ha_mode": ha_mode(),
            })
    return changed


def unpublish_records(rdb=None):
    rdb = rdb or _agent().redis_connect(privileged=True)
    module_id = env("MODULE_ID")
    rdb.delete(f"module/{module_id}/srv/tcp/{SERVICE}", f"module/{module_id}/srv/tcp/{CLUSTER_SERVICE}")


# ---------------------------------------------------------------------------
# Rendering on disk (ExecStartPre)
# ---------------------------------------------------------------------------

def _copy_tls(target_dir):
    os.makedirs(os.path.join(target_dir, "tls"), mode=0o700, exist_ok=True)
    changed = False
    for name in ("ca.crt", "node.crt", "node.key"):
        changed |= write_if_changed(os.path.join(target_dir, "tls", name), read_text(tls_path(name)))
    return changed


def render_etcd_dir_tls():
    ensure_node_cert(local_host())
    _copy_tls(state_path("etcd"))


def etcd_hosts(member_list):
    hosts = [endpoint("127.0.0.1", ports()["etcd_client"])]
    for m in member_list:
        if m["module_id"] != env("MODULE_ID"):
            hosts.append(endpoint(m["host"], m["etcd_client_port"]))
    return hosts


def render_component(component, member_list=None):
    """Render the files of a component under state/<component>. Returns
    True when a file changed."""
    host = local_host()
    p = ports()
    changed = ensure_node_cert(host)
    if component == "etcd":
        changed |= _copy_tls(state_path("etcd"))
        text = render_etcd_env({
            "module_id": env("MODULE_ID"), "host": host, "ports": p,
            "initial_cluster": env("PG_ETCD_INITIAL_CLUSTER"),
            "initial_state": env("PG_ETCD_INITIAL_STATE") or "new",
            "token": cluster_id(),
        })
        changed |= write_if_changed(state_path("etcd", "etcd.env"), text)
    elif component == "patroni":
        member_list = member_list if member_list is not None else cluster_members()
        changed |= _copy_tls(state_path("patroni"))
        secrets_values = ensure_secrets()
        text = render_patroni({
            "module_id": env("MODULE_ID"), "scope": cluster_id(), "host": host, "ports": p,
            "secrets": secrets_values, "etcd_hosts": etcd_hosts(member_list),
            # Cluster settings are applied through the DCS (apply_dynamic_config):
            # the bootstrap values stay fixed, so changing them restarts nothing
            "max_connections": DEFAULT_MAX_CONNECTIONS, "replication_mode": "async",
        })
        changed |= write_if_changed(state_path("patroni", "patroni.yml"), text)
    elif component == "pgproxy":
        member_list = member_list if member_list is not None else cluster_members()
        text = render_haproxy({
            "host": host, "ports": p,
            "members": [{"module_id": m["module_id"], "host": m["host"], "pg_port": m["pg_port"],
                         "patroni_port": m["patroni_port"]}
                        for m in member_list if runs_postgres(m["role"]) and m["pg_port"]],
        })
        changed |= write_if_changed(state_path("pgproxy", "haproxy.cfg"), text, 0o644)
    elif component == "pgadmin":
        changed |= write_if_changed(state_path("pgadmin", "servers.json"),
                                    render_pgadmin_servers({"module_id": env("MODULE_ID"), "ports": p}), 0o644)
        values = ensure_secrets()
        changed |= write_if_changed(state_path("pgadmin", "pgadmin.env"),
                                    f"PGADMIN_DEFAULT_PASSWORD={values['PGADMIN_PASSWORD']}\n")
    return changed


def set_marker(component, name="reset-data"):
    os.makedirs(state_path(component), exist_ok=True)
    with open(state_path(component, name), "w"):
        pass


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------

def other_etcd_endpoints(member_list):
    return [etcd_url(m["host"], m["etcd_client_port"]) for m in member_list
            if m["module_id"] != env("MODULE_ID")]


def promote_self(member_list, timeout=180):
    """Promote the local etcd learner to voting member once it caught up"""
    eps = other_etcd_endpoints(member_list) or None
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            mine = [m for m in etcd_members(eps) if m["name"] == env("MODULE_ID")]
            if mine and not mine[0]["learner"]:
                return True
            if mine:
                proc = etcdctl(["member", "promote", mine[0]["id"]], endpoints=eps)
                if proc.returncode == 0:
                    log(f"etcd member {env('MODULE_ID')} promoted to voter")
                    return True
                last = proc.stderr.strip()[-200:]
        except Exception as ex:
            last = str(ex)[-200:]
        time.sleep(5)
    warning(f"The etcd learner is not promoted yet ({last}): the periodic check retries")
    return False


def remove_etcd_member(name, member_list):
    """Remove the etcd member called `name` through the other members"""
    eps = [etcd_url(m["host"], m["etcd_client_port"]) for m in member_list if m["module_id"] != name]
    if not eps:
        return False
    for m in etcd_members(eps):
        if m["name"] == name:
            proc = etcdctl(["member", "remove", m["id"]], endpoints=eps)
            if proc.returncode != 0:
                raise RuntimeError(f"etcd member remove {name} failed: {proc.stderr.strip()[-200:]}")
            log(f"etcd member {name} removed")
            return True
    return False


def leave_cluster(member_list):
    """Hand the primary role over, stop PostgreSQL and remove this member
    from the etcd store. The caller then starts the instance as a new
    standalone with empty data."""
    others = [m for m in member_list if m["module_id"] != env("MODULE_ID")]
    if runs_postgres() and unit_active(UNITS["patroni"]):
        if is_local_primary() and not switchover_away():
            others_pg = [m for m in others if runs_postgres(m["role"])]
            if others_pg:
                raise RuntimeError("no healthy replica can take over the primary role")
        systemctl("stop", UNITS["patroni"], check=False)
    if others:
        try:
            remove_etcd_member(env("MODULE_ID"), member_list)
        except Exception as ex:
            warning(f"Cannot remove this member from etcd: {ex}")
    systemctl("stop", UNITS["etcd"], check=False)


def with_lock(name="membership"):
    os.makedirs(state_path("locks"), exist_ok=True)
    fd = os.open(state_path("locks", name), os.O_CREAT | os.O_RDWR, 0o600)
    fp = os.fdopen(fd, "r+")
    fcntl.flock(fp, fcntl.LOCK_EX)
    return fp


# ---------------------------------------------------------------------------
# Status (no secrets)
# ---------------------------------------------------------------------------

def cluster_status(rdb=None):
    """Health of this instance and of its cluster"""
    rdb = rdb or _agent().redis_connect(use_replica=True)
    member_list = cluster_members(rdb)
    status = {
        "role": role(), "ha_mode": ha_mode(), "cluster_id": cluster_id() if role() else "",
        "module_id": env("MODULE_ID"), "node": env("NODE_ID"),
        "replication_mode": replication_mode(), "version": PG_VERSION,
        "members": [], "etcd": [], "leader": "", "timeline": 0,
        "endpoint": {"host": local_host(), "port": ports()["endpoint"], "ro_port": ports()["endpoint_ro"]},
        "warnings": [],
    }
    if not role():
        status.update(quorum_summary(0, 0))
        status["warnings"] = ["not_configured"]
        return status
    patroni = patroni_cluster(member_list) or {}
    records = {m["module_id"]: m for m in member_list}
    for pm in patroni.get("members", []):
        lag = pm.get("lag", 0)
        entry = {
            "name": pm.get("name", ""), "role": pm.get("role", ""), "state": pm.get("state", ""),
            "host": pm.get("host", ""), "port": pm.get("port", 0), "timeline": pm.get("timeline", 0),
            "lag_bytes": lag if isinstance(lag, int) else -1,
            "node": records.get(pm.get("name"), {}).get("node", ""),
        }
        status["members"].append(entry)
        if entry["role"] in ("leader", "standby_leader"):
            status["leader"] = entry["name"]
            status["timeline"] = entry["timeline"]
    healthy = 0
    voters = 0
    learners = 0
    try:
        eps = [etcd_url(m["host"], m["etcd_client_port"]) for m in member_list]
        etcd_list = etcd_members(eps)
        health = {}
        proc = etcdctl(["endpoint", "health", "--cluster", "-w", "json"], endpoints=eps)
        try:
            for h in json.loads(proc.stdout or "[]"):
                health[h.get("endpoint", "")] = bool(h.get("health"))
        except ValueError:
            pass
        for m in etcd_list:
            ok = any(health.get(u) for u in m["client_urls"])
            status["etcd"].append({"name": m["name"], "learner": m["learner"], "healthy": ok,
                                   "peer_url": (m["peer_urls"] or [""])[0]})
            if m["learner"]:
                learners += 1
            else:
                voters += 1
                healthy += 1 if ok else 0
    except Exception as ex:
        warning(f"Cannot read the etcd members: {ex}")
        status["warnings"].append("etcd_unreachable")
    pg_members = len([m for m in member_list if runs_postgres(m["role"])])
    summary = quorum_summary(voters, healthy, learners, pg_members)
    status["warnings"] = status["warnings"] + summary.pop("warnings")
    status.update(summary)
    if not is_cluster():
        # A standalone instance is its own quorum: nothing to warn about
        status["warnings"] = [w for w in status["warnings"] if w not in ("below_quorum",)]
    if runs_postgres() and not status["leader"]:
        status["warnings"].append("no_leader")
    for entry in status["members"]:
        if entry["role"] not in ("leader", "standby_leader") and entry["state"] not in ("streaming", "running"):
            status["warnings"].append("replica_not_streaming")
            break
        if entry["lag_bytes"] > 16 * 1024 * 1024:
            status["warnings"].append("replica_lagging")
            break
    status["members_count"] = len(member_list)
    status["postgres_members"] = pg_members
    return status


# ---------------------------------------------------------------------------
# Units and reconciliation
# ---------------------------------------------------------------------------

def wanted_units(value=None, pgadmin=None):
    """Units that run for a role, in start order"""
    value = role() if value is None else value
    if not value:
        return []
    units = [UNITS["etcd"]]
    if runs_postgres(value):
        units.append(UNITS["patroni"])
    units.append(UNITS["pgproxy"])
    if (env("PGADMIN_ENABLED") == "1") if pgadmin is None else pgadmin:
        units.append(UNITS["pgadmin"])
    return units


def active_units():
    return [u for u in UNITS.values() if unit_active(u)]


def with_lock_nb(name="membership"):
    """Like with_lock(), None when another process holds the lock"""
    os.makedirs(state_path("locks"), exist_ok=True)
    fd = os.open(state_path("locks", name), os.O_CREAT | os.O_RDWR, 0o600)
    fp = os.fdopen(fd, "r+")
    try:
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fp.close()
        return None
    return fp


def module_installed(module_id, rdb):
    return bool(rdb.hget("cluster/module_node", module_id))


def reconcile(removed="", rdb=None):
    """One pass of the membership check (see bin/pgha-reconcile). Returns
    the list of changes made."""
    changes = []
    agent = _agent()
    wrdb = agent.redis_connect(privileged=True)
    rdb = rdb or agent.redis_connect(use_replica=True)
    if publish_records(wrdb):
        changes.append("records")
    member_list = cluster_members(rdb)
    if unit_active(UNITS["pgproxy"]) and render_component("pgproxy", member_list):
        systemctl("reload", UNITS["pgproxy"], check=False)
        changes.append("endpoint-members")
    if not is_cluster():
        return changes
    try:
        etcd_list = etcd_members(other_etcd_endpoints(member_list) + [etcd_url("127.0.0.1", ports()["etcd_client"])])
    except Exception as ex:
        warning(f"Cannot read the etcd members: {ex}")
        return changes
    mine = [m for m in etcd_list if m["name"] == env("MODULE_ID")]
    if mine and mine[0]["learner"] and promote_self(member_list, timeout=30):
        changes.append("etcd-promoted")
    # The primary removes the etcd members of removed modules: their quorum
    # vote would otherwise be missing forever
    if runs_postgres() and is_local_primary():
        known = {m["module_id"] for m in member_list}
        for m in etcd_list:
            name = m["name"]
            if not name or name in known:
                continue
            if name == removed or not module_installed(name, rdb):
                try:
                    if remove_etcd_member(name, member_list):
                        changes.append(f"etcd-removed-{name}")
                except Exception as ex:
                    warning(f"Cannot remove the etcd member {name}: {ex}")
    try:
        import pgha_secrets
        if pgha_secrets.sync_shared():
            changes.append("openbao-sync")
    except Exception as ex:
        warning(f"OpenBao sync skipped: {ex}")
    return changes


def reload_env():
    """Actions see the variables written by earlier steps only in the
    environment file: reload it (removed variables are written empty)"""
    os.environ.update(_agent().read_envfile(state_path("environment")))


WARNINGS_FILE = ".configure-warnings.json"


def add_warning(item):
    try:
        with open(state_path(WARNINGS_FILE)) as fp:
            items = json.load(fp)
    except (FileNotFoundError, ValueError):
        items = []
    items.append(item)
    write_private(state_path(WARNINGS_FILE), json.dumps(items))


def pop_warnings():
    try:
        with open(state_path(WARNINGS_FILE)) as fp:
            items = json.load(fp)
        os.unlink(state_path(WARNINGS_FILE))
        return items
    except (FileNotFoundError, ValueError):
        return []


def dcs_config(member_list=None):
    """Dynamic configuration of the cluster (GET /config), {} if unknown"""
    candidates = []
    if runs_postgres():
        candidates.append((local_host(), ports()["patroni"]))
    for m in member_list or []:
        if m.get("patroni_port") and runs_postgres(m.get("role")):
            candidates.append((m["host"], int(m["patroni_port"])))
    for host, port in candidates:
        try:
            status, data = rest(host, port, "/config", timeout=3)
            if status == 200 and isinstance(data, dict):
                return data
        except Exception:
            continue
    return {}


def dcs_setting(name, member_list=None):
    cfg = dcs_config(member_list)
    if name == "synchronous_mode":
        return cfg.get("synchronous_mode")
    return ((cfg.get("postgresql") or {}).get("parameters") or {}).get(name)


def client_endpoints(rdb=None):
    """[{module_id, node, host, port, ro_port}] of the client endpoints of
    this cluster: every instance runs one (witnesses included)"""
    rdb = rdb or _agent().redis_connect(use_replica=True)
    result = []
    if is_cluster():
        for rec in _agent().list_service_providers(rdb, SERVICE, "tcp"):
            if rec.get("cluster_id") != cluster_id() or rec.get("module_id") == env("MODULE_ID"):
                continue
            try:
                result.append({"module_id": rec["module_id"], "node": str(rec.get("node", "")),
                               "host": rec["host"], "port": int(rec["port"]),
                               "ro_port": int(rec.get("ro_port") or 0)})
            except (KeyError, ValueError):
                continue
    p = ports()
    result.append({"module_id": env("MODULE_ID"), "node": env("NODE_ID"), "host": local_host(),
                   "port": p["endpoint"], "ro_port": p["endpoint_ro"]})
    return sorted(result, key=lambda e: member_sort_key(e["module_id"]))


def pick_endpoint(endpoints, node=""):
    """The endpoint on the caller's node if there is one, else this
    instance's endpoint"""
    for e in endpoints:
        if node and e["node"] == str(node):
            return e
    for e in endpoints:
        if e["module_id"] == env("MODULE_ID"):
            return e
    return endpoints[0]


def run_reconcile(removed="", timer=False):
    """Locked reconciliation; returns {"result", "changes"[, "error"|"reason"]}"""
    if not role():
        return {"result": "skipped", "changes": [], "reason": "not_configured"}
    lock = with_lock_nb("membership") if timer else with_lock("membership")
    if lock is None:
        return {"result": "skipped", "changes": [], "reason": "busy"}
    try:
        changes = reconcile(removed=removed)
        if changes:
            log("Reconciled: " + ", ".join(changes))
        return {"result": "ok", "changes": changes}
    except Exception as ex:
        error(f"Reconciliation failed: {ex}")
        return {"result": "error", "changes": [], "error": str(ex)[:300]}
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
