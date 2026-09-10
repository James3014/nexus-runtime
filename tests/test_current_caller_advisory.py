from types import SimpleNamespace
import pytest
from nexus_runtime import build_runtime_exports


def parser(payload):
    return SimpleNamespace(metadata=payload['metadata'], authority=SimpleNamespace(value=payload.get('authority', 'advisory')), fallback_block_reason='verified_block')


def setup(advisory, *, planner_enabled=False):
    exports = build_runtime_exports(hybrid_route_decision_from_payload=parser)
    request = exports.UnifiedRuntimeRequest(task_id='task', workspace_revision='revision', task_statement='repair', task_type='repair', route={}, local_enabled=True, online_enabled=True, local_request={'task_id':'task'})
    plan = {'selected_capabilities':['local_model_executor'], 'planner_decision_id':'plan', 'plan_hash':'plan', 'signal_snapshot':{'fail_closed_enabled':planner_enabled}}
    payload = {'task_id':'task','action':'advisor','invoked':True,'local_model_invoked':True,'output_delivered':True,'evidence_refs':['fixture:physical-local'],'hybrid_route_advisory':advisory}
    runtime = exports.UnifiedRuntime(local_service=lambda request:payload)
    return exports, request, plan, runtime


def test_valid_advisory_is_successful_without_claiming_vap_closure():
    exports, request, plan, runtime = setup({'metadata':{'task_id':'task','planner_decision_id':'plan'}})
    result = runtime._run_local(request, plan, workforce_admission_required=False)
    assert result['status'] == 'SUCCEEDED'
    assert result['gate_passed'] is True
    assert result['outcome_contributed'] is False


@pytest.mark.parametrize('metadata,reason', [({'task_id':'other','planner_decision_id':'plan'},'advisory_task_identity_mismatch'),({'task_id':'task','planner_decision_id':'other'},'advisory_planner_identity_mismatch')])
def test_identity_mismatch_blocks_local_and_online(metadata,reason):
    exports, request, plan, runtime = setup({'metadata':metadata})
    local = runtime._run_local(request,plan,workforce_admission_required=False)
    assert local['status']=='FAILED'
    assert reason in local['reason']
    calls=[]
    online=runtime._run_online(request,lambda context:calls.append(context),{'task_id':'task','planner_decision_id':'plan','planner':plan,'local':local},workforce_admission_required=False)
    assert online['invoked'] is False
    assert reason in online['reason']
    assert calls==[]


@pytest.mark.parametrize('advisory',[None,[], 'invalid'])
def test_malformed_advisory_is_not_accepted(advisory):
    exports,request,plan,runtime=setup(advisory)
    local=runtime._run_local(request,plan,workforce_admission_required=False)
    assert local['status']=='FAILED'
    assert local['reason']=='invalid_local_advisory:advisory_payload_not_mapping'


@pytest.mark.parametrize('enabled,reason',[(False,'fail_closed_override_not_planner_enabled'),(True,'verified_block')])
def test_fail_closed_override_stays_planner_bound(enabled,reason):
    exports,request,plan,runtime=setup({'metadata':{'task_id':'task','planner_decision_id':'plan'},'authority':'fail_closed'},planner_enabled=enabled)
    local=runtime._run_local(request,plan,workforce_admission_required=False)
    assert local['status']=='FAILED'
    assert reason in local['reason']
    calls=[]
    online=runtime._run_online(request,lambda context:calls.append(context),{'task_id':'task','planner_decision_id':'plan','planner':plan,'local':local},workforce_admission_required=False)
    assert online['invoked'] is False
    assert reason in online['reason']
    assert calls==[]
