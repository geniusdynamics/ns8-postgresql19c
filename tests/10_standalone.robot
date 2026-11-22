*** Settings ***
Documentation    Standalone server: install, configure, a database for a
...              consumer, backups, secrets, removal.
Library    SSHLibrary
Resource    postgresql.resource

*** Variables ***
${mid}    ${EMPTY}

*** Test Cases ***
Install the module
    ${id} =    Install Instance
    Set Suite Variable    ${mid}    ${id}
    Copy Probe

Nothing runs before configure-module
    ${config} =    Run Action    ${mid}    get-configuration
    Should Be Equal    ${config}[role]    ${EMPTY}
    Should Not Be True    ${config}[configured]

Configure a standalone server
    ${out} =    Run Action    ${mid}    configure-module    {"role": "standalone"}
    ${status} =    Wait Until Keyword Succeeds    180s    5s    Status Should Be Healthy    ${mid}    1    1
    Should Be Equal    ${status}[leader]    ${mid}
    Should Be Equal    ${status}[ha_mode]    standalone
    Should Not Contain    ${status}[warnings]    below_quorum

PostgreSQL 19 runs under Patroni
    ${output}  ${rc} =    Execute Command    runagent -m ${mid} podman exec -u postgres patroni psql -tAc 'SELECT version()'
    ...    return_rc=True
    Should Be Equal As Integers    ${rc}  0
    Should Contain    ${output}    PostgreSQL 19
    ${output} =    Execute Command    runagent -m ${mid} podman exec -u postgres patroni psql -tAc 'SHOW password_encryption'
    Should Be Equal    ${output}    scram-sha-256

The services listen on loopback and the VPN address only
    ${config} =    Run Action    ${mid}    get-configuration
    ${port} =    Set Variable    ${config}[endpoint][port]
    ${output} =    Execute Command    ss -Htln "sport = :${port}"
    Should Contain    ${output}    127.0.0.1:${port}
    Should Not Contain    ${output}    0.0.0.0:${port}
    Should Not Contain    ${output}    *:${port}

Secrets are private files, not environment
    ${output}  ${rc} =    Execute Command    runagent -m ${mid} bash -c 'cd $AGENT_STATE_DIR && stat -c %a secrets.env tls/ca.key patroni/patroni.yml'
    ...    return_rc=True
    Should Be Equal As Integers    ${rc}  0
    Should Be Equal    ${output}    600\n600\n600
    ${env} =    Execute Command    runagent -m ${mid} bash -c 'cat $AGENT_STATE_DIR/environment'
    Should Not Contain    ${env}    PASSWORD
    ${config} =    Execute Command    api-cli run module/${mid}/get-configuration
    Output Should Not Contain Secrets    ${mid}    ${config}
    Should Contain    ${config}    "superuser_password_set": true

A consumer gets a database
    ${grant} =    Grant Database    ${mid}    {"client_id": "robotapp"}
    Should Be Equal    ${grant}[database]    robotapp
    Should Be Equal    ${grant}[username]    robotapp
    Should Be Equal    ${grant}[ha_mode]    standalone
    Should Be Equal As Integers    ${grant}[password_length]    64
    Probe Should Succeed    ${mid}    CREATE TABLE t (v text); INSERT INTO t VALUES ('hello');
    ${out} =    Probe Should Succeed    ${mid}    SELECT v FROM t;
    Should Be Equal    ${out}    hello

The consumer reaches only its database
    Execute Command    python3 -c 'import json; f="/root/pg-grant.json"; g=json.load(open(f)); g["database"]="postgres"; json.dump(g, open("/root/pg-grant-other.json", "w"))'
    ${output} =    Execute Command    runagent -m ${mid} python3 /tmp/pg-probe.py "SELECT 1" < /root/pg-grant-other.json
    ${result} =    Evaluate    json.loads($output)    modules=json
    Should Not Be True    ${result}[ok]
    Execute Command    rm -f /root/pg-grant-other.json

A new grant rotates the password
    Execute Command    cp -p /root/pg-grant.json /root/pg-grant-old.json
    Grant Database    ${mid}    {"client_id": "robotapp"}
    ${output} =    Execute Command    runagent -m ${mid} python3 /tmp/pg-probe.py "SELECT 1" < /root/pg-grant-old.json
    ${result} =    Evaluate    json.loads($output)    modules=json
    Should Not Be True    ${result}[ok]
    ${out} =    Probe Should Succeed    ${mid}    SELECT v FROM t;
    Should Be Equal    ${out}    hello
    Execute Command    rm -f /root/pg-grant-old.json

Another client cannot take the database
    ${output} =    Action Should Fail    ${mid}    grant-database    {"client_id": "intruder", "database": "robotapp"}
    Should Contain    ${output}    database_owned_by_other

Databases are listed without secrets
    ${list} =    Run Action    ${mid}    list-databases
    ${names} =    Evaluate    [d["name"] for d in $list["databases"]]
    Should Contain    ${names}    robotapp
    ${raw} =    Execute Command    api-cli run module/${mid}/list-databases
    Should Not Contain    ${raw}    password

The backup dump comes from the primary
    ${output}  ${err}  ${rc} =    Execute Command    runagent -m ${mid} module-dump-state
    ...    return_rc=True    return_stderr=True
    Should Be Equal As Integers    ${rc}  0    ${err}
    ${dump} =    Execute Command    runagent -m ${mid} bash -c 'grep -c -E "^CREATE ROLE .?robotapp.?;" $AGENT_STATE_DIR/postgresql.dump'
    Should Be Equal    ${dump}    1
    ${sys} =    Execute Command    runagent -m ${mid} bash -c 'grep -c -E "^CREATE ROLE .?(postgres|replicator|rewind_user).?;" $AGENT_STATE_DIR/postgresql.dump'
    Should Be Equal    ${sys}    0
    Execute Command    runagent -m ${mid} module-cleanup-state

Revoke keeps the data and disables the login
    ${out} =    Run Action    ${mid}    revoke-database    {"client_id": "robotapp"}
    Should Be True    ${out}[revoked]
    ${result} =    Probe    ${mid}    SELECT 1;
    Should Not Be True    ${result}[ok]
    ${list} =    Run Action    ${mid}    list-databases
    ${db} =    Evaluate    [d for d in $list["databases"] if d["name"] == "robotapp"][0]
    Should Not Be True    ${db}[owner_can_login]

A restart keeps the data
    Execute Command    runagent -m ${mid} pgha-restart
    Wait Until Keyword Succeeds    180s    5s    Status Should Be Healthy    ${mid}    1    1
    Grant Database    ${mid}    {"client_id": "robotapp"}
    ${out} =    Wait Until Keyword Succeeds    60s    5s    Probe Should Succeed    ${mid}    SELECT v FROM t;
    Should Be Equal    ${out}    hello

Remove the module
    ${rc} =    Execute Command    remove-module --no-preserve ${mid}
    ...    return_rc=True  return_stdout=False
    Should Be Equal As Integers    ${rc}  0
    Execute Command    rm -f /root/pg-grant.json
