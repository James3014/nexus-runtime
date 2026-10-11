from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .context import KernelContext


def _assert_runtime_receipt_owner(ctx: KernelContext, owner_context: Any, path: Path) -> None:
    """Validate an opted-in receipt write through the shared owner context.

        Runtime keeps its legacy receipt path when no context is supplied.  When
        an owner transaction is supplied, the shared state-owner adapter is the
        authority for the exact role/path and must reject before any parent
        directory creation or receipt bytes.
        """
    if owner_context is None:
        return
    assert_owner_write = ctx.assert_owner_write

    try:
        root = owner_context.binding.root.resolve(strict=True)
        if not path.is_absolute():
            raise ValueError("receipt path must be absolute for an owner write")
        relative_path = path.relative_to(root).as_posix()
        cursor = root
        for part in Path(relative_path).parts[:-1]:
            cursor = cursor / part
            if cursor.exists() or cursor.is_symlink():
                info = cursor.lstat()
                if not cursor.is_dir() or cursor.is_symlink():
                    raise ValueError("runtime receipt parent is unsafe")
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if path.is_symlink() or not path.is_file() or not stat.S_ISREG(info.st_mode):
                raise ValueError("runtime receipt target is unsafe")
    except (AttributeError, OSError, ValueError) as exc:
        raise ValueError("runtime_receipt_owner_path_invalid") from exc
    assert_owner_write(owner_context, role="runtime_receipt", relative_path=relative_path)


def _validate_runtime_owner_context(ctx: KernelContext, owner_context: Any, receipt_path: str | Path | None) -> None:
    if owner_context is None:
        return
    if receipt_path is None:
        raise ValueError("runtime_receipt_path_required")
    _assert_runtime_receipt_owner(ctx, owner_context, Path(receipt_path))


def _write_receipt_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


@contextlib.contextmanager
def _runtime_write_context(
    ctx: KernelContext,
    runtime_writer_factory: Any,
    *,
    task_id: str,
    role: str,
    path: Path,
    owner_context: Any = None,
):
    """Enter a fresh source-owned lease only at the durable write boundary."""
    if owner_context is not None and runtime_writer_factory is not None:
        raise ValueError("runtime_owner_context_and_factory_conflict")
    if owner_context is not None:
        yield owner_context
        return
    if runtime_writer_factory is not None:
        RuntimeWriterFactory = ctx.RuntimeWriterFactory
        if not isinstance(runtime_writer_factory, RuntimeWriterFactory):
            raise ValueError("runtime_writer_factory_invalid")
        runtime_writer_factory.validate_entry(path=path, role=role)
        with runtime_writer_factory.for_operation(task_id, role=role, path=path) as context:
            yield context
        return
    _validate_runtime_writer_entry(
        ctx,
        None,
        owner_context=None,
        receipt_path=path if role == "runtime_receipt" else None,
        effect_journal=None,
        selected_effect_path=path if role == "effect_journal" else None,
    )
    yield None


def _assert_runtime_context_active(factory: Any, context: Any) -> None:
    if factory is not None:
        factory.assert_context(context)


def _validate_runtime_effect_owner(ctx: KernelContext, owner_context: Any, effect_journal: Any) -> None:
    if owner_context is None or effect_journal is None:
        return
    EffectJournal = ctx.EffectJournal
    assert_owner_write = ctx.assert_owner_write

    if not isinstance(effect_journal, EffectJournal):
        return
    try:
        binding_root = owner_context.binding.root.resolve(strict=True)
        journal_root = effect_journal.project_root.resolve(strict=True)
        if journal_root != binding_root:
            raise ValueError("runtime_effect_owner_root_mismatch")
        if effect_journal.generation != owner_context.writer_generation:
            raise ValueError("runtime_effect_owner_generation_mismatch")
        assert_owner_write(
            owner_context,
            role="effect_journal",
            relative_path=".nexus/events/effect_journal.v1.json",
        )
    except (AttributeError, OSError, ValueError) as exc:
        if str(exc).startswith("runtime_effect_owner_"):
            raise
        raise ValueError("runtime_effect_owner_binding_invalid") from exc


def _validate_runtime_writer_entry(
    ctx: KernelContext,
    runtime_writer_factory: Any,
    *,
    owner_context: Any,
    receipt_path: str | Path | None,
    effect_journal: Any,
    selected_effect_path: Path | None = None,
) -> None:
    """Fail closed before Planner/model/effect work for an activated root."""
    if runtime_writer_factory is not None:
        RuntimeWriterFactory = ctx.RuntimeWriterFactory
        if not isinstance(runtime_writer_factory, RuntimeWriterFactory):
            raise ValueError("runtime_writer_factory_invalid")
        if owner_context is not None:
            raise ValueError("runtime_owner_context_and_factory_conflict")
        if receipt_path is not None:
            runtime_writer_factory.validate_entry(path=receipt_path, role="runtime_receipt")
        if effect_journal is not None:
            runtime_writer_factory.validate_entry(path=effect_journal.path, role="effect_journal")
        return
    if owner_context is not None:
        return
    candidates: list[Path] = []
    if receipt_path is not None:
        candidates.append(Path(receipt_path))
    if effect_journal is not None and hasattr(effect_journal, "project_root"):
        candidates.append(Path(effect_journal.project_root))
    if selected_effect_path is not None:
        candidates.append(selected_effect_path)
    if not candidates:
        return
    candidates = [variant for path in candidates for variant in (path.absolute(), path.resolve())]
    read_manifest = ctx.read_manifest
    read_generation = ctx.read_generation
    receipt_mode = receipt_path is not None
    for candidate in candidates:
        root = candidate if candidate.is_dir() else candidate.parent
        while True:
            marker = root / ".nexus" / "writer-quiescence-hold.json"
            try:
                held = marker.is_symlink() or marker.exists()
            except OSError:
                held = True
            if held or read_manifest(root) is not None or (receipt_mode and read_generation(root) is not None):
                raise ValueError("runtime_writer_factory_required")
            if root == root.parent:
                break
            root = root.parent
