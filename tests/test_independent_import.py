def test_installed_package_imports_without_nexus_new_checkout():
    import nexus_runtime

    exports = nexus_runtime.build_runtime_exports()
    assert "UnifiedRuntime" in exports.names()


def test_context_checkpoint_search_and_readback(tmp_path):
    from nexus_context_prototype import ContextContinuityService, Scope
    from nexus_context_prototype.access import AllowAllFixturePolicy

    service = ContextContinuityService(
        tmp_path / "context",
        authenticated_principal="test-principal",
        access_policy=AllowAllFixturePolicy(),
    )
    scope = Scope("project", "repo", "task")
    item = service.ingest(
        scope,
        session_id="session",
        window_id="window",
        producer_id="producer",
        role="note",
        payload=b"checkpoint search payload",
        source_type="test",
        idempotency_key="item-1",
        subject_revision="rev-1",
    )
    service.checkpoint(
        scope,
        session_id="session",
        window_id="window",
        producer_id="producer",
        hint="resume here",
        item_refs=(item.item_id,),
    )
    assert service.resume(scope).item_refs == (item.item_id,)
    assert service.search(scope, "search").items[0].item_id == item.item_id
    assert service.read_item(scope, item.item_id).payload == b"checkpoint search payload"


def test_context_acl_denies_read_and_write(tmp_path):
    from nexus_context_prototype import ContextContinuityService, Scope

    class DenyAll:
        def can_read(self, scope, principal):
            return False

        def can_write(self, scope, principal):
            return False

    service = ContextContinuityService(
        tmp_path / "context",
        authenticated_principal="principal",
        access_policy=DenyAll(),
    )
    scope = Scope("project", "repo", "task")
    import pytest

    with pytest.raises(PermissionError, match="SCOPE_DENIED"):
        service.search(scope, "query")
