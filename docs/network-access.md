# Network access

Part of the [ns8-postgresql19c documentation](../README.md).

Other NethServer modules always reach PostgreSQL through the client
endpoint on the cluster VPN (or loopback): nothing in this page is needed
for them. These settings let people and applications **outside** the
NethServer cluster reach the databases. They apply **per instance**: in a
cluster, each node decides its own exposure.

| Setting | Default | Effect |
|---|---|---|
| `pgadmin_enabled` | off | pgAdmin runs and gets a Traefik route; off: container stopped, route deleted |
| `host`, `pgadmin_path` | | the route: a host name, a path (e.g. `/pgadmin`), or both |
| `lets_encrypt` | off | certificate for the host name |
| `pgadmin_ip_allowlist` | empty | Traefik `ip_allowlist` of the route; empty: any source |
| `public_access_enabled` | off | opens the public endpoint port in the node firewall |
| `public_access_allowlist` | empty | sources allowed on the public endpoint; empty: any source |

```bash
api-cli run module/postgresql19c1/configure-module --data '{
  "role": "initial",
  "pgadmin_enabled": true, "host": "pgadmin.example.org", "lets_encrypt": true,
  "pgadmin_ip_allowlist": ["192.0.2.0/24"],
  "public_access_enabled": true, "public_access_allowlist": ["203.0.113.10", "198.51.100.0/24"]
}'
```

## pgAdmin

- HTTPS only (`http2https` is always on). Its login is
  `pgadmin_email` (default `admin@nethserver.org`) and a generated password
  in `state/secrets.env` (0600); `get-configuration` returns
  `pgadmin_password_set`, and administrators read the initial password
  with `reveal-pgadmin-password` (the *Settings* page has a button). pgAdmin
  uses it at its first start only: change it in pgAdmin.
- pgAdmin connects to the local read-write endpoint (`127.0.0.1`), so in a
  cluster it always reaches the primary.
- With a path, pgAdmin runs with `SCRIPT_NAME=<path>` and Traefik keeps the
  prefix.

## Direct database access

Each instance has a **public endpoint** (8th TCP port, `PUBLIC_PORT`),
rendered only while `public_access_enabled` is on. It is a listener of the
same HAProxy as the client endpoint, on every IPv4 address of the node, and
routes to the current primary, so in a cluster any node's public endpoint
reaches the primary.

Firewall (core API, authorization `node:fwadm`):

- empty allowlist: `add-public-service` opens `PUBLIC_PORT/tcp` in the
  public zone (service `<module_id>-public`);
- allowlist: no open service; `add-rich-rules` adds one firewalld rich rule
  per source (`rule family="ipv4" source address="<net>" port
  port="<port>" protocol="tcp" accept`);
- off: the rules and the service are removed (`state/firewall.json` keeps
  the applied rules so that exactly those are removed). `destroy-module`
  closes everything.

The proxy enforces the same rules a second time, because it sees the real
client address:

- `acl allowed_src src <allowlist>` rejects other sources at connection;
- **TLS only**: the first packet must be the PostgreSQL `SSLRequest` (or a
  direct TLS ClientHello, libpq `sslnegotiation=direct`); plain-text
  connections are closed.

Why not `pg_hba.conf`: the endpoint is a TCP proxy, so PostgreSQL sees
every connection coming from the proxy's VPN address. Source addresses and
"external" connections cannot be told apart there; the firewall and the
proxy can. `pg_hba.conf` stays SCRAM-only for every connection.

### TLS

PostgreSQL runs with `ssl = on` on every member (TLS 1.2 or later), with
the member certificate signed by the cluster CA. Internal clients may keep
`sslmode=disable` or `prefer` (the VPN encrypts); replication between
members uses TLS too.

Clients of the public endpoint use `sslmode=verify-ca` (or `require`) and
the CA certificate from `get-configuration` `tls_ca_certificate` (shown on
the *Settings* page):

```bash
psql "host=pg.example.org port=<PUBLIC_PORT> dbname=myapp user=myapp sslmode=verify-ca sslrootcert=ca.crt"
```

`verify-full` is not offered: the connection is passed through to the
primary, which may be another node than the one the client connects to,
so the certificate cannot carry the name the client used. For the same
reason a Let's Encrypt certificate from Traefik is not used. The CA is
private to the cluster, so `verify-ca` still authenticates the server.

### Limits

- IPv4 only on the public endpoint.
- Restore always comes back with direct access off (the firewall belongs
  to the node); turn it on again. A clone keeps the setting and opens the
  port on its own node.
- The Robot suite checks the firewall rules and the proxy allowlist from
  the test node itself; a second source address cannot be faked there, so
  "another source is refused" is checked by removing the node's address
  from the allowlist.
