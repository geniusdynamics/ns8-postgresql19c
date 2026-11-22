*** Settings ***
Documentation    A cluster of three instances on the test node (allowed by
...              allow_same_node, for tests only): approval and join, a
...              witness, replication, the client endpoint following the
...              primary across a failover and a switchover, a member
...              leaving, removal.
Library    SSHLibrary
Library    Collections
Resource    postgresql.resource

*** Variables ***
${m1}    ${EMPTY}
${m2}    ${EMPTY}
${m3}    ${EMPTY}
${m4}    ${EMPTY}

*** Test Cases ***
Install four instances
    ${a} =    Install Instance
    ${b} =    Install Instance
    ${c} =    Install Instance
    ${d} =    Install Instance
    Set Suite Variable    ${m1}    ${a}
    Set Suite Variable    ${m2}    ${b}
    Set Suite Variable    ${m3}    ${c}
    Set Suite Variable    ${m4}    ${d}
    Copy Probe

Create the cluster with data on the first instance
    Run Action    ${m1}    configure-module    {"role": "standalone"}
    Wait Until Keyword Succeeds    180s    5s    Status Should Be Healthy    ${m1}    1    1
    Grant Database    ${m1}    {"client_id": "robotha"}
    Probe Should Succeed    ${m1}    CREATE TABLE t (id serial primary key, v text); INSERT INTO t (v) VALUES ('standalone');
    # Standalone to cluster in place: the data stays
    Run Action    ${m1}    configure-module    {"role": "initial", "allow_same_node": true}
    ${status} =    Wait Until Keyword Succeeds    120s    5s    Status Should Be Healthy    ${m1}    1    1
    Should Be Equal    ${status}[ha_mode]    cluster
    Should Contain    ${status}[warnings]    below_quorum
    ${out} =    Probe Should Succeed    ${m1}    SELECT v FROM t;
    Should Be Equal    ${out}    standalone
    ${config} =    Run Action    ${m2}    get-configuration
    ${ids} =    Evaluate    [c["cluster_id"] for c in $config["cluster_candidates"]]
    Should Contain    ${ids}    ${m1}

Joining without approval is refused
    ${output} =    Action Should Fail    ${m2}    configure-module    {"role": "join", "cluster_id": "${m1}"}
    Should Contain    ${output}    not_admitted
    ${config} =    Run Action    ${m2}    get-configuration
    Should Be Equal    ${config}[role]    ${EMPTY}

Approve and join a replica
    Run Action    ${m1}    set-peer-access    {"peer": "${m2}", "allowed": true}
    ${config} =    Run Action    ${m1}    get-configuration
    ${allowed} =    Evaluate    [p["module_id"] for p in $config["peer_candidates"] if p["allowed"]]
    Should Contain    ${allowed}    ${m2}
    Run Action    ${m2}    configure-module    {"role": "join", "cluster_id": "${m1}"}
    ${status} =    Wait Until Keyword Succeeds    300s    5s    Status Should Be Healthy    ${m1}    2    2
    Should Contain    ${status}[warnings]    below_quorum
    Should Be Equal As Integers    ${status}[failure_tolerance]    0

Approve and join a second replica
    Run Action    ${m1}    set-peer-access    {"peer": "${m3}", "allowed": true}
    Run Action    ${m3}    configure-module    {"role": "join", "cluster_id": "${m1}"}
    ${status} =    Wait Until Keyword Succeeds    300s    5s    Status Should Be Healthy    ${m1}    3    3
    Should Be Equal As Integers    ${status}[failure_tolerance]    1
    Should Not Contain    ${status}[warnings]    below_quorum
    Should Be Equal    ${status}[leader]    ${m1}

Add a witness
    Run Action    ${m2}    set-peer-access    {"peer": "${m4}", "allowed": true}
    Run Action    ${m4}    configure-module    {"role": "witness", "cluster_id": "${m1}"}
    ${status} =    Wait Until Keyword Succeeds    180s    5s    Status Should Be Healthy    ${m1}    3    4
    Should Contain    ${status}[warnings]    even_voters
    ${units} =    Execute Command    runagent -m ${m4} systemctl --user is-active patroni.service
    Should Not Be Equal    ${units}    active

Joining twice is refused
    ${output} =    Action Should Fail    ${m2}    configure-module    {"role": "initial"}
    Should Contain    ${output}    leave_cluster_first

The data is replicated and every endpoint reaches the primary
    Wait Until Keyword Succeeds    60s    5s    Replicas Have Row    standalone
    FOR    ${mid}    IN    ${m1}    ${m2}    ${m3}    ${m4}
        ${config} =    Run Action    ${mid}    get-configuration
        Use Endpoint    ${config}[endpoint][host]    ${config}[endpoint][port]
        ${out} =    Wait Until Keyword Succeeds    60s    5s    Probe Should Succeed    ${mid}    INSERT INTO t (v) VALUES ('via-${mid}') RETURNING v;
        Should Be Equal    ${out}    via-${mid}
        ${ro} =    Probe Should Succeed    ${mid}    SELECT count(*) FROM t;    ro_port
    END

A grant on a replica works for the whole cluster
    ${grant} =    Grant Database    ${m3}    {"client_id": "robotha"}
    Should Be Equal    ${grant}[ha_mode]    cluster
    Length Should Be    ${grant}[endpoints]    4
    ${out} =    Probe Should Succeed    ${m3}    SELECT v FROM t ORDER BY id LIMIT 1;
    Should Be Equal    ${out}    standalone

Reader output holds no secret
    FOR    ${action}    IN    get-configuration    get-cluster-status    list-databases
        ${raw} =    Execute Command    api-cli run module/${m2}/${action}
        Output Should Not Contain Secrets    ${m2}    ${raw}
    END

Failover: the primary stops, a replica is promoted, the endpoint follows
    ${old} =    Leader Of    ${m2}
    Execute Command    runagent -m ${old} podman kill --signal=KILL patroni
    Execute Command    runagent -m ${old} systemctl --user stop patroni.service
    ${survivor} =    Set Variable If    '${old}' == '${m3}'    ${m2}    ${m3}
    ${new} =    Wait Until Keyword Succeeds    90s    3s    Leader Should Change    ${survivor}    ${old}
    ${config} =    Run Action    ${m4}    get-configuration
    Use Endpoint    ${config}[endpoint][host]    ${config}[endpoint][port]
    ${out} =    Wait Until Keyword Succeeds    60s    3s    Probe Should Succeed    ${m4}    INSERT INTO t (v) VALUES ('after-failover') RETURNING v;
    Should Be Equal    ${out}    after-failover
    Set Suite Variable    ${old_leader}    ${old}

The old primary rejoins as a replica
    Execute Command    runagent -m ${old_leader} systemctl --user start patroni.service
    ${status} =    Wait Until Keyword Succeeds    300s    5s    Status Should Be Healthy    ${m1}    3    4
    Should Not Be Equal    ${status}[leader]    ${old_leader}
    Wait Until Keyword Succeeds    60s    5s    Replicas Have Row    after-failover

A rolling restart switches the primary over first
    ${leader} =    Leader Of    ${m1}
    ${rc} =    Execute Command    runagent -m ${leader} pgha-restart
    ...    return_rc=True  return_stdout=False
    Should Be Equal As Integers    ${rc}  0
    ${status} =    Wait Until Keyword Succeeds    300s    5s    Status Should Be Healthy    ${m1}    3    4
    Should Not Be Equal    ${status}[leader]    ${leader}

Synchronous replication is a cluster setting
    Run Action    ${m2}    configure-module    {"role": "join", "cluster_id": "${m1}", "replication_mode": "sync"}
    ${config} =    Wait Until Keyword Succeeds    60s    5s    Replication Mode Should Be    ${m3}    sync
    Run Action    ${m3}    configure-module    {"role": "join", "cluster_id": "${m1}", "replication_mode": "async"}
    Wait Until Keyword Succeeds    60s    5s    Replication Mode Should Be    ${m1}    async

The backup dump runs only on the primary
    ${leader} =    Leader Of    ${m1}
    FOR    ${mid}    IN    ${m1}    ${m2}    ${m3}    ${m4}
        Execute Command    runagent -m ${mid} module-dump-state
        ${first} =    Execute Command    runagent -m ${mid} bash -c 'head -c 200 $AGENT_STATE_DIR/postgresql.dump'
        IF    '${mid}' == '${leader}'
            Should Not Contain    ${first}    no dump on this member
        ELSE
            Should Contain    ${first}    no dump on this member
        END
        Execute Command    runagent -m ${mid} module-cleanup-state
    END

A member leaves the cluster
    ${leader} =    Leader Of    ${m1}
    ${leaving} =    Set Variable If    '${leader}' == '${m3}'    ${m2}    ${m3}
    Run Action    ${leaving}    configure-module    {"role": "standalone"}
    ${status} =    Wait Until Keyword Succeeds    180s    5s    Status Should Be Healthy    ${m1}    2    3
    ${config} =    Run Action    ${leaving}    get-configuration
    Should Be Equal    ${config}[role]    standalone
    Should Be Equal    ${config}[cluster_id]    ${leaving}
    ${solo} =    Wait Until Keyword Succeeds    180s    5s    Status Should Be Healthy    ${leaving}    1    1
    Set Suite Variable    ${left}    ${leaving}

Removing a member removes its etcd vote
    ${rc} =    Execute Command    remove-module --no-preserve ${m4}
    ...    return_rc=True  return_stdout=False
    Should Be Equal As Integers    ${rc}  0
    ${status} =    Wait Until Keyword Succeeds    180s    5s    Status Should Be Healthy    ${m1}    2    2
    ${out} =    Wait Until Keyword Succeeds    60s    5s    Probe Should Succeed    ${m1}    SELECT count(*) > 0 FROM t;    port

Remove the instances
    FOR    ${mid}    IN    ${m1}    ${m2}    ${m3}
        ${rc} =    Execute Command    remove-module --no-preserve ${mid}
        ...    return_rc=True  return_stdout=False
        Should Be Equal As Integers    ${rc}  0
    END
    Execute Command    rm -f /root/pg-grant.json

*** Keywords ***
Leader Should Change
    [Arguments]    ${via}    ${old}
    ${status} =    Run Action    ${via}    get-cluster-status
    Should Not Be Empty    ${status}[leader]
    Should Not Be Equal    ${status}[leader]    ${old}
    RETURN    ${status}[leader]

Replicas Have Row
    [Arguments]    ${value}
    FOR    ${mid}    IN    ${m1}    ${m2}    ${m3}
        ${role} =    Execute Command    runagent -m ${mid} podman exec -u postgres patroni psql -tAc "SELECT count(*) FROM t WHERE v = '${value}'" robotha
        Should Be True    int('${role}' or 0) >= 1    ${mid}: ${role}
    END

Replication Mode Should Be
    [Arguments]    ${mid}    ${mode}
    ${config} =    Run Action    ${mid}    get-configuration
    Should Be Equal    ${config}[replication_mode]    ${mode}
