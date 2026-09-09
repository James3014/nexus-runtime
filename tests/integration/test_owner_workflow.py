import hashlib, json, tempfile
from pathlib import Path
import pytest

pytest.importorskip("nexus_learning")
pytest.importorskip("product")
pytest.importorskip("repository_intelligence")
pytest.importorskip("nexus_open_swe_runtime")
from nexus_runtime_support_candidate import build_runtime_exports
from nexus_learning.episode_projection import project_learning_entries
from repository_intelligence.impact import analyze_change_impact, verify_change_impact_report
from nexus_open_swe_runtime import cli as openswe_cli
from product import kernel as product_kernel

def test_runtime_learning_workflow():
    exports=build_runtime_exports()
    with tempfile.TemporaryDirectory() as d:
        root=Path(d); artifact=root/'candidate.txt'; local_receipt=root/'local.json'; receipt=root/'receipt.json'; ledger=root/'.nexus/reports/learn/learning_closure.jsonl'; ledger.parent.mkdir(parents=True)
        artifact.write_text('before', encoding='utf-8')
        impact = analyze_change_impact({'changed_files': ['candidate.txt'], 'edges': [{'source': 'candidate.txt', 'target': 'candidate.txt'}], 'symbols': [{'file': 'candidate.txt', 'name': 'main'}]})
        assert (impact.to_dict() if hasattr(impact, 'to_dict') else dict(impact))['changed_files']
        request=exports.UnifiedRuntimeRequest(task_id='six-repo',workspace_revision='rev',task_statement='deterministic local integration',task_type='repair',route={'recommended_flow':'direct','online_policy':'deny','local_enabled':True},online_enabled=False,local_enabled=True,local_request={'task_id':'six-repo','action':'candidate'})
        plan=exports.CapabilityPlanner().plan(task_desc=request.task_statement,task_type=request.task_type,route=dict(request.route),pillars={},codeintel={},phase_trace={},budget={},skills=[])
        inv={n:(lambda c,selected=n:{'task_id':c['task_id'],'invoked':True,'gate_passed':True,'evidence_refs':[f'harness:{selected}']}) for n in plan.selected_capabilities}
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        class GraphModel(BaseChatModel):
            model_name: str = 'harness'
            @property
            def _llm_type(self): return 'harness'
            def _get_ls_params(self, *args, **kwargs): return {'ls_provider':'test','ls_model_name':self.model_name}
            def bind_tools(self, tools, **kwargs): return self
            def _generate(self, messages, **kwargs):
                if any(getattr(m, 'type', '') == 'tool' for m in messages): return ChatResult(generations=[ChatGeneration(message=AIMessage(content='written'))])
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content='', tool_calls=[{'name':'write_file','args':{'file_path':'candidate.txt','content':'graph-result'},'id':'call'}]))])
        graph=openswe_cli.build_repair_graph(GraphModel(), root, openswe_cli._load_runtime(), ('candidate.txt',), 'six-repo')
        tamper = {'enabled': False}
        def local(c):
            graph.invoke({'messages':[{'role':'user','content':'write candidate.txt'}]}, config={'recursion_limit':12}); h='sha256:'+hashlib.sha256(artifact.read_bytes()).hexdigest(); local_receipt.write_text(json.dumps({'task_id':c['task_id'],'terminal_status':'SUCCEEDED','receipt_complete':True,'verifier_result':'pass','candidate_hashes':[h]}));
            if tamper['enabled']: artifact.write_text('tampered', encoding='utf-8')
            return {'schema':'nexus.local_assist.response.v1','task_id':c['task_id'],'action':'candidate','invoked':True,'local_model_invoked':True,'output_delivered':True,'executor_invoked':True,'physical_callable':'LocalModelExecutor.run','candidate_summary':{'isolation_status':'isolated','selected_candidate_hash':h,'selected_candidate_hash_matches_applied':True},'claim_boundary':{'local_model_executor_invoked':True},'receipt_path':str(local_receipt),'evidence_refs':['harness:local']}
        def verifier(c):
            ok=artifact.read_text()=='graph-result'; return {'status':'SUCCEEDED' if ok else 'FAILED','task_id':c['task_id'],'invoked':True,'gate_passed':ok,'verifier_status':'pass' if ok else 'fail','verifier_artifact':'sha256:'+hashlib.sha256(artifact.read_bytes()).hexdigest(),'source_hash':str(c.get('source_hash') or ''),'evidence_refs':['harness:verifier']}
        result=exports.UnifiedRuntime(local_service=local).run(request,capability_invokers=inv,verifier=verifier,learning=lambda c:{'status':'SUCCEEDED','task_id':c['task_id'],'invoked':True,'gate_passed':True,'evidence_refs':['harness:learning']},receipt_path=receipt)
        assert result['terminal_status']=='SUCCEEDED'; readback=json.loads(receipt.read_text()); assert readback['task_id']=='six-repo'
        from product.evidence import AcceptanceContract, ChangeSet, EvidenceBundle, Observation, ObservationStatus, VerificationPlan, _hash
        from product.kernel import CertificationInput, certify
        digest = _hash(artifact.read_text(encoding='utf-8'))
        contract=AcceptanceContract('six-repo', _hash('artifact'), ('unit',), ('candidate.txt',), 'FORBID')
        change=ChangeSet('six-change', 'before', 'after', digest, ('candidate.txt',)); plan_obj=VerificationPlan('six-plan', contract.hash, change.hash, ('unit',))
        cert=certify(CertificationInput(contract, change, plan_obj, EvidenceBundle('six-evidence', contract.hash, change.hash, plan_obj.hash, (Observation('unit', 'runtime-receipt', digest, ObservationStatus.PASS),)), True, True, True, True))
        assert cert.receipt is not None
        ledger.write_text(json.dumps({'task_id':'six-repo','summary':'deterministic local integration','classification':'success','provenance':str(receipt)})+'\n')
        assert project_learning_entries(json.loads(line) for line in ledger.read_text().splitlines())
        tamper['enabled'] = True
        failed = exports.UnifiedRuntime(local_service=local).run(request, capability_invokers=inv, verifier=verifier, learning=lambda c: {'status':'SUCCEEDED','task_id':c['task_id'],'invoked':True,'gate_passed':True,'evidence_refs':['harness:learning']}, receipt_path=root/'tampered-receipt.json')
        assert failed['terminal_status'] != 'SUCCEEDED'

def test_external_owner_surfaces_are_installed_and_execute_local_analysis():
    report = analyze_change_impact({"changed_files": [], "edges": [], "symbols": []})
    payload = report.to_dict() if hasattr(report, "to_dict") else dict(report)
    assert verify_change_impact_report(payload)
    assert callable(openswe_cli.build_semantic_graph)
    assert callable(product_kernel.certify)

def test_openswe_builds_real_graphs_with_deterministic_chat_model(tmp_path):
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    class Model(BaseChatModel):
        model_name: str = "deterministic"
        @property
        def _llm_type(self): return "deterministic-open-swe"
        def _get_ls_params(self, *args, **kwargs): return {"ls_provider": "test", "ls_model_name": self.model_name}
        def bind_tools(self, tools, **kwargs): return self
        def _generate(self, messages, **kwargs): return ChatResult(generations=[ChatGeneration(message=AIMessage(content="done"))])
    runtime = openswe_cli._load_runtime()
    model = Model()
    semantic = openswe_cli.build_semantic_graph(model, tmp_path, runtime, "six-repo")
    repair = openswe_cli.build_repair_graph(model, tmp_path, runtime, ("candidate.txt",), "six-repo")
    assert openswe_cli.SEMANTIC_TOOLS.issubset(set(openswe_cli.executable_tool_surface(semantic)))
    assert openswe_cli.REPAIR_TOOLS.issubset(set(openswe_cli.executable_tool_surface(repair)))
    output = semantic.invoke({"messages": [{"role": "user", "content": "inspect local repository"}]})
    assert output is not None
    class ToolModel(Model):
        def _generate(self, messages, **kwargs):
            if any(getattr(message, "type", "") == "tool" for message in messages):
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content="written"))])
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[{"name": "write_file", "args": {"file_path": "candidate.txt", "content": "graph-result"}, "id": "graph-call"}]))])
    tool_graph = openswe_cli.build_repair_graph(ToolModel(), tmp_path, runtime, ("candidate.txt",), "six-repo-tool")
    tool_output = tool_graph.invoke({"messages": [{"role": "user", "content": "write candidate.txt"}]}, config={"recursion_limit": 12})
    assert tool_output is not None
    assert (tmp_path / "candidate.txt").read_text(encoding="utf-8") == "graph-result"
def test_core_certifies_a_real_workflow_artifact():
    from product.evidence import AcceptanceContract, ChangeSet, EvidenceBundle, Observation, ObservationStatus, VerificationPlan, _hash
    from product.kernel import CertificationInput, certify
    contract = AcceptanceContract("workflow", _hash("requirement"), ("unit",), ("candidate.txt",), "FORBID")
    change = ChangeSet("change", "before", "after", _hash("candidate"), ("candidate.txt",))
    plan = VerificationPlan("plan", contract.hash, change.hash, ("unit",))
    evidence = EvidenceBundle("evidence", contract.hash, change.hash, plan.hash, (Observation("unit", "artifact", _hash("pass"), ObservationStatus.PASS),))
    result = certify(CertificationInput(contract, change, plan, evidence, True, True, True, True))
    assert result.receipt is not None
