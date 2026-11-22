*** Settings ***
Documentation    Optional OpenBao: the shared secrets are copied to a KV v2
...              store (a fake OpenBao server on the test node), the local
...              copy keeps the instance working while OpenBao is sealed,
...              and OpenBao is turned off with the OpenBao-only secrets
...              copied back.
Library    SSHLibrary
Resource    postgresql.resource

*** Variables ***
${mid}      ${EMPTY}
${PORT}     18299
${TOKEN}    robot-openbao-token

*** Test Cases ***
Start a fake OpenBao and an instance
    Put File    ${CURDIR}/fake_openbao.py    /tmp/fake_openbao.py    mode=0644
    Execute Command    systemd-run --unit=fake-openbao-pg --collect python3 /tmp/fake_openbao.py ${PORT} ${TOKEN}
    ${id} =    Install Instance
    Set Suite Variable    ${mid}    ${id}
    Copy Probe
    Run Action    ${mid}    configure-module    {"role": "standalone"}
    Wait Until Keyword Succeeds    180s    5s    Status Should Be Healthy    ${mid}    1    1

A wrong token is refused
    ${output} =    Action Should Fail    ${mid}    configure-module    {"role": "standalone", "vault_enabled": true, "vault_address": "http://127.0.0.1:${PORT}", "vault_token": "wrong"}
    Should Contain    ${output}    openbao_denied

Turn OpenBao on
    Run Action    ${mid}    configure-module    {"role": "standalone", "vault_enabled": true, "vault_address": "http://127.0.0.1:${PORT}", "vault_token": "${TOKEN}", "vault_mount": "secret", "vault_path": "pgrobot"}
    ${config} =    Run Action    ${mid}    get-configuration
    Should Be True    ${config}[vault_enabled]
    Should Be True    ${config}[vault_token_set]
    ${raw} =    Execute Command    api-cli run module/${mid}/get-configuration
    Should Not Contain    ${raw}    ${TOKEN}
    ${mode} =    Execute Command    runagent -m ${mid} bash -c 'stat -c %a $AGENT_STATE_DIR/openbao.env'
    Should Be Equal    ${mode}    600
    ${env} =    Execute Command    runagent -m ${mid} bash -c 'cat $AGENT_STATE_DIR/environment'
    Should Not Contain    ${env}    ${TOKEN}
    ${value} =    Read Bao    pgrobot/${mid}/PG_REPLICATION_PASSWORD
    ${local} =    Execute Command    runagent -m ${mid} bash -c 'grep ^PG_REPLICATION_PASSWORD= $AGENT_STATE_DIR/secrets.env | cut -d= -f2'
    Should Be Equal    ${value}    ${local}

Consumer credentials are published in OpenBao
    ${grant} =    Grant Database    ${mid}    {"client_id": "baoapp"}
    Should Be Equal    ${grant}[openbao_path]    pgrobot/${mid}/client/baoapp
    ${list} =    Run Action    ${mid}    list-openbao-only-secrets
    ${names} =    Evaluate    [i["name"] for i in $list["items"]]
    Should Contain    ${names}    client/baoapp
    Should Be True    ${list}[copyable]

PostgreSQL restarts while OpenBao is sealed
    Execute Command    curl -s -X POST http://127.0.0.1:${PORT}/fake/seal
    Execute Command    runagent -m ${mid} pgha-restart
    Wait Until Keyword Succeeds    180s    5s    Status Should Be Healthy    ${mid}    1    1
    ${out} =    Wait Until Keyword Succeeds    60s    5s    Probe Should Succeed    ${mid}    SELECT 1;
    Should Be Equal    ${out}    1
    ${result} =    Execute Command    runagent -m ${mid} pgha-reconcile
    Should Contain    ${result}    "result": "ok"
    Execute Command    curl -s -X POST http://127.0.0.1:${PORT}/fake/unseal

Turning OpenBao off copies the OpenBao-only secrets back
    ${out} =    Run Action    ${mid}    configure-module    {"role": "standalone", "vault_enabled": false, "vault_copy_to_local": true}
    Should Be Empty    ${out}[warnings]
    ${config} =    Run Action    ${mid}    get-configuration
    Should Not Be True    ${config}[vault_enabled]
    Should Not Be True    ${config}[vault_token_set]
    Should Be Empty    ${config}[vault_only_secrets]
    ${mode} =    Execute Command    runagent -m ${mid} bash -c 'stat -c %a $AGENT_STATE_DIR/secrets/clients/baoapp'
    Should Be Equal    ${mode}    600

Turning OpenBao off without copying warns
    Run Action    ${mid}    configure-module    {"role": "standalone", "vault_enabled": true, "vault_address": "http://127.0.0.1:${PORT}", "vault_token": "${TOKEN}", "vault_mount": "secret", "vault_path": "pgrobot"}
    Grant Database    ${mid}    {"client_id": "baoapp2"}
    ${out} =    Run Action    ${mid}    configure-module    {"role": "standalone", "vault_enabled": false}
    ${ids} =    Evaluate    [w["id"] for w in $out["warnings"]]
    Should Contain    ${ids}    openbao_items_unavailable

Clean up
    ${rc} =    Execute Command    remove-module --no-preserve ${mid}
    ...    return_rc=True  return_stdout=False
    Should Be Equal As Integers    ${rc}  0
    Execute Command    systemctl stop fake-openbao-pg
    Execute Command    rm -f /root/pg-grant.json /tmp/fake_openbao.py

*** Keywords ***
Read Bao
    [Arguments]    ${path}
    ${out} =    Execute Command    curl -s -H 'X-Vault-Token: ${TOKEN}' http://127.0.0.1:${PORT}/v1/secret/data/${path} | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["data"]["value"])'
    RETURN    ${out}
