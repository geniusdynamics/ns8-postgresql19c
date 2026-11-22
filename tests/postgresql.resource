*** Settings ***
Library    SSHLibrary

*** Keywords ***
Install Instance
    ${output}  ${rc} =    Execute Command    add-module ${IMAGE_URL} 1
    ...    return_rc=True
    Should Be Equal As Integers    ${rc}  0    add-module failed: ${output}
    &{output} =    Evaluate    ${output}
    RETURN    ${output.module_id}

Run Action
    [Arguments]    ${module_id}    ${action}    ${data}={}
    ${output}  ${err}  ${rc} =    Execute Command    api-cli run module/${module_id}/${action} --data '${data}'
    ...    return_rc=True    return_stderr=True
    Should Be Equal As Integers    ${rc}  0    ${action} on ${module_id} failed: ${output} ${err}
    IF    $output.strip() == '' or $output.strip() == 'null'
        RETURN    ${NONE}
    END
    ${result} =    Evaluate    json.loads($output)    modules=json
    RETURN    ${result}

Action Should Fail
    [Arguments]    ${module_id}    ${action}    ${data}={}
    ${output}  ${err}  ${rc} =    Execute Command    api-cli run module/${module_id}/${action} --data '${data}'
    ...    return_rc=True    return_stderr=True
    Should Not Be Equal As Integers    ${rc}  0
    RETURN    ${output}${err}

Copy Probe
    Put File    ${CURDIR}/pg-probe.py    /tmp/pg-probe.py    mode=0644

Grant Database
    [Documentation]    Run grant-database and keep its output (password included) in a
    ...                0600 file of the test node. Returns the output without the password.
    [Arguments]    ${module_id}    ${data}    ${file}=/root/pg-grant.json
    ${rc} =    Execute Command    umask 077; api-cli run module/${module_id}/grant-database --data '${data}' > ${file}
    ...    return_rc=True  return_stdout=False
    Should Be Equal As Integers    ${rc}  0    grant-database on ${module_id} failed
    ${output} =    Execute Command    python3 -c 'import json,sys; g=json.load(open(sys.argv[1])); g["password_length"]=len(g.pop("password")); g.pop("url"); print(json.dumps(g))' ${file}
    ${result} =    Evaluate    json.loads($output)    modules=json
    RETURN    ${result}

Use Endpoint
    [Documentation]    Point the saved grant at the client endpoint of another instance
    [Arguments]    ${host}    ${port}    ${file}=/root/pg-grant.json
    Execute Command    python3 -c 'import json,sys; f=sys.argv[1]; g=json.load(open(f)); g["host"]=sys.argv[2]; g["port"]=int(sys.argv[3]); json.dump(g, open(f, "w"))' ${file} ${host} ${port}

Probe
    [Documentation]    Run SQL as the consumer, from the module user of an instance
    [Arguments]    ${module_id}    ${sql}    ${port_key}=port    ${file}=/root/pg-grant.json
    ${output}  ${rc} =    Execute Command    runagent -m ${module_id} python3 /tmp/pg-probe.py "${sql}" ${port_key} < ${file}
    ...    return_rc=True
    Should Be Equal As Integers    ${rc}  0    ${output}
    ${result} =    Evaluate    json.loads($output)    modules=json
    RETURN    ${result}

Probe Should Succeed
    [Arguments]    ${module_id}    ${sql}    ${port_key}=port
    ${result} =    Probe    ${module_id}    ${sql}    ${port_key}
    Should Be True    ${result}[ok]    ${result}
    RETURN    ${result}[output]

Status Should Be Healthy
    [Arguments]    ${module_id}    ${members}    ${voters}
    ${status} =    Run Action    ${module_id}    get-cluster-status
    Should Not Be Empty    ${status}[leader]
    Should Be True    ${status}[quorum_ok]
    Should Be Equal As Integers    ${status}[voters]    ${voters}
    ${running} =    Evaluate    [m for m in $status["members"] if m["state"] in ("running", "streaming")]
    Length Should Be    ${running}    ${members}
    RETURN    ${status}

Leader Of
    [Arguments]    ${module_id}
    ${status} =    Run Action    ${module_id}    get-cluster-status
    Should Not Be Empty    ${status}[leader]
    RETURN    ${status}[leader]

Output Should Not Contain Secrets
    [Arguments]    ${module_id}    ${data}
    ${secrets} =    Execute Command    runagent -m ${module_id} bash -c 'cat $AGENT_STATE_DIR/secrets.env'
    FOR    ${line}    IN    @{secrets.splitlines()}
        ${value} =    Evaluate    $line.split("=", 1)[1]
        Should Not Contain    ${data}    ${value}
    END
