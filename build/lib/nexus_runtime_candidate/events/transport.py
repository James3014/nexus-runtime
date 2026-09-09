from __future__ import annotations
from __future__ import annotations

import logging

import os

import stat

import threading

import time

import uuid

from contextlib import contextmanager

from dataclasses import replace

from pathlib import Path

from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from ..events.contracts import AttemptTransitionEvent

from ..events.log_store import DeveloperFeedbackDecisionStore, JsonlEventLogStore, event_owner_context

from ..events.writer_generation import EventWriterGeneration, GenerationError

from ..feedback.contracts import DeveloperFeedbackDecision

class EventWriterAdapter:
    """Operation-scoped event-log binding over an already loaded registry."""

    def __init__(self, registry, *, binding, writer_generation, root, writer_id, initial_attachment=None):
        from nexus_runtime_p6c_candidate.events.state_owner_manifest import StateOwnerBinding
        from nexus_runtime_p6c_candidate.events.writer_generation import EventWriterGeneration
        from nexus_runtime_p6c_candidate.orchestrator.writer_quiescence import UnknownWriter
        from nexus_runtime_p6c_candidate.orchestrator.writer_quiescence import WriterRegistry
        from nexus_runtime_p6c_candidate.orchestrator.writer_quiescence import _root
        if not isinstance(registry, WriterRegistry):
            raise TypeError('registry is required')
        if not isinstance(binding, StateOwnerBinding):
            raise TypeError('source-owned state owner binding is required')
        if not isinstance(writer_generation, EventWriterGeneration):
            raise TypeError('source-owned writer generation is required')
        canonical = _root(root)
        if binding.root.resolve() != Path(canonical) or writer_generation.generation != binding.generation or writer_generation.writer_id != writer_id:
            raise UnknownWriter('event writer root or generation does not match binding')
        self.registry = registry
        self.binding = binding
        self.writer_generation = writer_generation
        self.root = canonical
        self.writer_id = writer_id
        self._root_identity = self._physical_identity()
        self._active_contexts = {}
        self._initial_attachment = initial_attachment
        if initial_attachment is not None:
            initial_attachment.verify(registry, root=canonical, generation=writer_generation, writer_id=writer_id, manifest_sha256=initial_attachment.manifest_sha256)
            registry.register_initial_writer(initial_attachment, role='event_log', writer_id=writer_id)
        self._identity()

    def _physical_identity(self):
        root = Path(self.root)
        info = root.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError('event writer root is unsafe')
        return (info.st_dev, info.st_ino)

    def _identity(self):
        from nexus_runtime_p6c_candidate.orchestrator.writer_quiescence import UnknownWriter
        from nexus_runtime_p6c_candidate.orchestrator.writer_quiescence import WriterIdentity
        item = self.registry._writers.get((self.root, 'event_log', self.writer_id))
        if item is None or item.identity.generation != self.writer_generation.generation:
            if self._initial_attachment is not None:
                self._initial_attachment.verify(self.registry, root=self.root, generation=self.writer_generation, writer_id=self.writer_id, manifest_sha256=self._initial_attachment.manifest_sha256)
                return WriterIdentity(self.root, 'event_log', self.registry.source_identity, self.registry.process_start_identity, str(threading.get_ident()), self.writer_generation.generation, self.writer_id)
            raise UnknownWriter('event writer is unknown or stale')
        if item.loaded_identity is None:
            raise UnknownWriter('event writer loaded identity is unavailable')
        loaded = item.loaded_identity()
        if loaded != item.identity:
            raise UnknownWriter('loaded event writer identity changed')
        return item.identity

    def validate_entry(self):
        from nexus_runtime_p6c_candidate.events.state_owner_manifest import read_manifest
        from nexus_runtime_p6c_candidate.events.writer_generation import read_generation
        from nexus_runtime_p6c_candidate.orchestrator.writer_quiescence import UnknownWriter
        from nexus_runtime_p6c_candidate.orchestrator.writer_quiescence import WriterAdmissionDenied
        if os.getpid() != self.registry._pid:
            raise UnknownWriter('event writer belongs to another process')
        if self._physical_identity() != self._root_identity:
            raise WriterAdmissionDenied('event writer root physical identity changed')
        marker = Path(self.root) / '.nexus' / 'writer-quiescence-hold.json'
        try:
            info = marker.lstat()
        except FileNotFoundError:
            info = None
        if info is not None:
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise WriterAdmissionDenied('event writer hold marker is unsafe')
            raise WriterAdmissionDenied('event writer root is held')
        installed = read_generation(Path(self.root))
        if installed != self.writer_generation:
            raise UnknownWriter('event writer generation is stale')
        manifest = read_manifest(Path(self.root))
        if manifest is None or manifest.state != 'COMMITTED':
            raise UnknownWriter('event writer owner manifest is unavailable')
        if manifest.owner_id != self.binding.owner_id or manifest.generation != self.writer_generation.generation or manifest.writer_id != self.writer_generation.writer_id:
            raise UnknownWriter('event writer owner binding mismatch')
        self._identity()

    def assert_context(self, context):
        from nexus_runtime_p6c_candidate.events.state_owner_manifest import assert_owner_write
        lease = self._active_contexts.get(id(context))
        if lease is None:
            raise RuntimeError('event writer context is not active')
        lease.validate()
        self._identity()
        if os.getpid() != context.owner_pid or threading.get_ident() != context.owner_thread_id:
            raise RuntimeError('event writer context thread mismatch')
        if self._physical_identity() != self._root_identity:
            raise RuntimeError('event writer root physical identity changed')
        assert_owner_write(context, role='event_log', relative_path='.nexus/events/event_log.jsonl')

    @contextmanager
    def operation(self, event_id: str, *, operation_id: str | None=None, transaction_id: str | None=None):
        from nexus_runtime_p6c_candidate.events.state_owner_manifest import StateOwnerSelection
        from nexus_runtime_p6c_candidate.events.state_owner_manifest import commit_owner_transaction
        from nexus_runtime_p6c_candidate.events.state_owner_manifest import owner_transaction_guard
        from nexus_runtime_p6c_candidate.events.state_owner_manifest import read_manifest
        from nexus_runtime_p6c_candidate.events.writer_generation import event_store_lock
        from nexus_runtime_p6c_candidate.orchestrator.writer_quiescence import WriterAdmissionDenied
        self.validate_entry()
        lease = self.registry.acquire(root=self.root, role='event_log', writer_id=self._identity().writer_id, operation_id=operation_id, transaction_id=transaction_id, generation=self.writer_generation.generation)
        prepared = False
        try:
            with event_store_lock(Path(self.root)):
                lease.validate()
                self.validate_entry()
                previous = read_manifest(Path(self.root))
                if previous is None or previous.state != 'COMMITTED':
                    raise WriterAdmissionDenied('event writer owner manifest is unavailable')
                op_binding = replace(self.binding, transaction_id=lease.transaction_id)
                selection = StateOwnerSelection(f'event:{event_id}', 'event_log', '.nexus/events/event_log.jsonl')
                with owner_transaction_guard(op_binding, writer_generation=self.writer_generation, selections=(selection,), previous_manifest_sha256=previous.manifest_sha256) as context:
                    prepared = True
                    lease.validate()
                    self._active_contexts[id(context)] = lease
                    try:
                        with event_owner_context(context):
                            yield context
                            self.assert_context(context)
                            committed = commit_owner_transaction(context)
                            if read_manifest(Path(self.root)) != committed:
                                raise WriterAdmissionDenied('event writer owner readback mismatch')
                            lease.validate()
                    finally:
                        self._active_contexts.pop(id(context), None)
            lease.close('committed')
        except BaseException:
            if not lease._closed:
                lease.close('unresolved' if prepared else 'failed')
            raise
    __call__ = operation

class EventWriterFactory:

    def __init__(self, adapter: EventWriterAdapter):
        if not isinstance(adapter, EventWriterAdapter):
            raise TypeError('event writer adapter is required')
        self._adapter = adapter

    def validate_entry(self):
        self._adapter.validate_entry()

    def assert_context(self, context):
        self._adapter.assert_context(context)

    def for_operation(self, event_id: str, **kwargs):
        return self._adapter.operation(event_id, **kwargs)
    __call__ = for_operation

_LOADED_EVENT_WRITER_FACTORIES: dict[str, EventWriterFactory] = {}

def register_event_writer_factory(factory: EventWriterFactory) -> EventWriterFactory:
    if not isinstance(factory, EventWriterFactory):
        raise TypeError('event writer factory is required')
    root = factory._adapter.root
    existing = _LOADED_EVENT_WRITER_FACTORIES.get(root)
    if existing is not None and existing is not factory:
        raise RuntimeError('event writer factory already loaded for root')
    if os.getpid() != factory._adapter.registry._pid:
        raise RuntimeError('event writer factory belongs to another process')
    _LOADED_EVENT_WRITER_FACTORIES[root] = factory
    return factory

def lookup_event_writer_factory(project_root: str | Path) -> EventWriterFactory | None:
    try:
        root = str(Path(project_root).expanduser().resolve())
    except (TypeError, ValueError, OSError):
        return None
    return _LOADED_EVENT_WRITER_FACTORIES.get(root)

def load_event_writer_factory(registry, *, binding, writer_generation, root: str | Path, writer_id: str, initial_attachment=None) -> EventWriterFactory:
    return register_event_writer_factory(EventWriterFactory(EventWriterAdapter(registry, binding=binding, writer_generation=writer_generation, root=root, writer_id=writer_id, initial_attachment=initial_attachment)))

from dataclasses import dataclass
from typing import Mapping
from ..ports import TransportBindings, require_complete_bindings

@dataclass(frozen=True, slots=True)
class TransportExports:
    _values: Mapping[str, object]
    def __getattr__(self, name: str) -> object:
        try: return self._values[name]
        except KeyError as exc: raise AttributeError(name) from exc
    def names(self) -> tuple[str, ...]: return tuple(sorted(self._values))

def build_transport(bindings: TransportBindings) -> TransportExports:
    _nexus_generated_bindings = require_complete_bindings(bindings, TransportBindings)
    SignalQueueService = _nexus_generated_bindings.SignalQueueService
    artifact_to_packet = _nexus_generated_bindings.artifact_to_packet
    artifact_transport_receipt = _nexus_generated_bindings.artifact_transport_receipt
    logger = logging.getLogger(__name__)

    if TYPE_CHECKING:
        from nexus_runtime_p6c_candidate.orchestrator.writer_quiescence import InitialWriterAttachment


    SEMANTIC_EVENT_TYPES = frozenset({'audit_failed', 'learning_decision', 'evidence_accepted', 'healing_artifact_announced', 'lifecycle_hook', 'phase_transition', 'spec_bind', 'attempt_transition'})

    RAW_EVENT_TYPES = frozenset({'phase_start', 'phase_end', 'lifecycle_pre', 'external_signal_injected', 'persist_event', 'test_event'})

    class NexusEventBus:
        """Persistent pub/sub with bidirectional signal injection."""
        _subscribers: Dict[str, List[Callable[[Dict[str, Any]], None]]] = {}
        _event_log_path: Optional[Path] = None
        _signal_queue: List[Dict[str, Any]] = []
        _remote_broadcaster: Optional[Callable[[str, Dict[str, Any]], None]] = None
        _sequence_lock = threading.RLock()
        _subs_lock = threading.Lock()
        _observer_lock = threading.RLock()
        _global_seq = 0
        _log_store = JsonlEventLogStore()
        _writer_factory: EventWriterFactory | None = None
        _developer_feedback_store = DeveloperFeedbackDecisionStore()
        _signal_queue_svc = SignalQueueService()
        _observer_error_count = 0
        _last_observer_error: Optional[Dict[str, Any]] = None
        _attempt_sequences: Dict[tuple[str, str], int] = {}
        _production_event_root: Optional[Path] = None
        _configured_event_root: Optional[Path] = None
        _event_bind_mode: Optional[str] = None

        @classmethod
        def set_remote_broadcaster(cls, broadcaster: Callable[[str, Dict[str, Any]], None]) -> None:
            cls._remote_broadcaster = broadcaster

        @classmethod
        def configure(cls, project_root: Path, *, create: bool=True, production: bool=False, writer_generation: Optional[EventWriterGeneration]=None, enforce_generation: bool=False, writer_factory: Optional[EventWriterFactory]=None, initial_handle: InitialWriterAttachment | None=None) -> None:
            """初始化持久化路徑"""
            project_root = Path(project_root).expanduser().resolve()
            if cls._production_event_root is not None and cls._production_event_root != project_root:
                raise RuntimeError('conflicting production event root')
            if production:
                cls._production_event_root = project_root
            log_dir, event_log_path = cls._log_store.configure(project_root, create=create, writer_generation=writer_generation, enforce_generation=enforce_generation, writer_factory=writer_factory, initial_handle=initial_handle)
            cls._event_log_path = event_log_path
            cls._writer_factory = writer_factory
            cls._configured_event_root = project_root
            cls._event_bind_mode = 'write' if create else 'read'
            if create:
                cls._developer_feedback_store.configure(project_root)
            with cls._sequence_lock:
                cls._attempt_sequences = {key: tail for key in cls._attempt_sequences for tail in [cls._log_store.attempt_tail(*key)] if tail}
            signal_file = log_dir / 'signal_inbox.jsonl'
            cls._signal_queue = cls._signal_queue_svc.load_from_inbox(signal_file)

        @classmethod
        def ensure_configured(cls, project_root: Path, *, production: bool=False) -> None:
            """Bind the event store once, upgrading a read-only bind when needed."""
            project_root = Path(project_root).expanduser().resolve()
            if cls._configured_event_root == project_root and cls._event_log_path is not None and (cls._event_bind_mode == 'write'):
                if production:
                    cls._production_event_root = project_root
                return
            cls.configure(project_root, create=True, production=production)

        @classmethod
        def _sync_signal_queue_from_legacy(cls) -> None:
            """Keep legacy direct writes to _signal_queue compatible with service state."""
            if cls._signal_queue is not cls._signal_queue_svc.queue:
                cls._signal_queue = cls._signal_queue_svc.reset(cls._signal_queue)

        @classmethod
        def subscribe(cls, event_type: str, handler: Callable[[Dict[str, Any]], None]) -> None:
            with cls._subs_lock:
                cls._subscribers.setdefault(event_type, []).append(handler)

        @classmethod
        def publish(cls, event_type: str, payload: Dict[str, Any]) -> None:
            """發布事件（v23 終極原子修復版）"""
            if event_type == 'developer_feedback_decision':
                raise ValueError('developer feedback decisions require the typed emitter')
            with cls._sequence_lock:
                cls._global_seq += 1
                local_payload = payload.copy()
                local_payload['_seq'] = cls._global_seq
                local_payload['internal_ts'] = time.time()
                local_payload.setdefault('_trace_id', str(uuid.uuid4()))
                record = {'event_type': event_type, 'timestamp': local_payload['internal_ts'], 'seq': local_payload['_seq'], 'payload': local_payload}
                if cls._event_log_path:
                    if cls._log_store.event_log_path != cls._event_log_path:
                        if cls._writer_factory is not None:
                            raise GenerationError('EVENT_WRITER_PATH_MISMATCH')
                        cls._log_store.event_log_path = cls._event_log_path
                    if cls._writer_factory is None:
                        cls._log_store.append_record(record)
                    else:
                        with cls._writer_factory.for_operation(f"publish-{local_payload['_seq']}", transaction_id=f"event-{local_payload['_seq']}-{uuid.uuid4().hex}"):
                            cls._log_store.append_record(record)
            with cls._subs_lock:
                handlers = cls._subscribers.get(event_type, [])[:]
            for handler in handlers:
                try:
                    handler(local_payload)
                except Exception as exc:
                    cls._record_observer_error(event_type, 'subscriber', exc)
            if cls._remote_broadcaster:
                try:
                    cls._remote_broadcaster(event_type, local_payload)
                except Exception as e:
                    cls._record_observer_error(event_type, 'remote_broadcaster', e)

        @classmethod
        def emit_attempt_transition(cls, event: AttemptTransitionEvent) -> Dict[str, Any]:
            """Append one ordered semantic attempt event without hidden payloads."""
            key = (event.task_id, event.attempt_id)
            with cls._sequence_lock:
                previous = cls._attempt_sequences.get(key, 0)
                if previous and event.sequence != previous + 1:
                    raise ValueError('attempt transition sequence must be contiguous')
                payload = event.to_dict()
                cls.publish('attempt_transition', payload)
                cls._attempt_sequences[key] = event.sequence
            return payload

        @classmethod
        def next_attempt_sequence(cls, task_id: str, attempt_id: str) -> int:
            """Return the only valid next sequence after persisted-tail recovery."""
            key = (task_id, attempt_id)
            with cls._sequence_lock:
                persisted = cls._log_store.attempt_tail(task_id, attempt_id)
                previous = max(cls._attempt_sequences.get(key, 0), persisted)
                return previous + 1

        @classmethod
        def emit_developer_feedback_decision(cls, decision: DeveloperFeedbackDecision, *, expected_tail: Optional[str]=None) -> Dict[str, Any]:
            """Persist a typed recommendation, then notify observers after commit."""
            record = cls._developer_feedback_store.append(decision, expected_tail=expected_tail)
            if getattr(record, 'replayed', False):
                return record
            payload = dict(record)
            with cls._subs_lock:
                handlers = cls._subscribers.get('developer_feedback_decision', [])[:]
            for handler in handlers:
                try:
                    handler(payload)
                except Exception as exc:
                    cls._record_observer_error('developer_feedback_decision', 'subscriber', exc)
            if cls._remote_broadcaster:
                try:
                    cls._remote_broadcaster('developer_feedback_decision', payload)
                except Exception as exc:
                    cls._record_observer_error('developer_feedback_decision', 'remote_broadcaster', exc)
            return record
        emit_feedback_decision = emit_developer_feedback_decision

        @classmethod
        def _record_observer_error(cls, event_type: str, observer: str, error: Exception) -> None:
            """Record observer failures without changing route or enforcement state."""
            with cls._observer_lock:
                cls._observer_error_count += 1
                cls._last_observer_error = {'event_type': event_type, 'observer': observer, 'error_type': type(error).__name__, 'error': str(error), 'observer_only': True}
            logger.exception('Observer error for %s (%s)', event_type, observer)

        @classmethod
        def observer_telemetry(cls) -> Dict[str, Any]:
            with cls._observer_lock:
                return {'schema': 'nexus.event_observer_telemetry.v1', 'observer_error_count': cls._observer_error_count, 'last_observer_error': dict(cls._last_observer_error or {}), 'enforcement_authority': 'synchronous_lifecycle_guards', 'observer_only': True}

        @classmethod
        def emit_audit_failure(cls, *, task_id: str, reason: str, evidence_id: str='') -> None:
            cls.publish('audit_failed', {'task_id': task_id, 'reason': reason, 'evidence_id': evidence_id})

        @classmethod
        def emit_learning_decision(cls, *, task_id: str, action: str, reasons: List[str] | None=None) -> None:
            cls.publish('learning_decision', {'task_id': task_id, 'action': action, 'reasons': list(reasons or [])})

        @classmethod
        def emit_evidence_accepted(cls, *, task_id: str, evidence_id: str, evidence_type: str='') -> None:
            cls.publish('evidence_accepted', {'task_id': task_id, 'evidence_id': evidence_id, 'evidence_type': evidence_type})

        @classmethod
        def emit_healing_artifact_announced(cls, *, artifact: Any, policy: Any) -> Dict[str, Any]:
            """Publish portable healing advice only after signature/key policy passes."""
            artifact_to_packet = _nexus_generated_bindings.artifact_to_packet
            artifact_transport_receipt = _nexus_generated_bindings.artifact_transport_receipt
            receipt = artifact_transport_receipt(artifact, policy)
            if not receipt.get('passed'):
                return receipt
            cls.publish('healing_artifact_announced', {'task_id': artifact.task_id, 'artifact_id': artifact.artifact_id, 'receipt': receipt, 'packet': artifact_to_packet(artifact)})
            return receipt

        @classmethod
        def inject_signal(cls, signal_type: str, payload: Dict[str, Any]) -> None:
            """外部注入信號（由 bot/人工/Pilot Friend 呼叫）"""
            cls._sync_signal_queue_from_legacy()
            cls._signal_queue = cls._signal_queue_svc.inject(signal_type, payload)
            cls.publish('external_signal_injected', {'signal_type': signal_type})

        @classmethod
        def drain_signals(cls, signal_type: str='') -> List[Dict[str, Any]]:
            """消費信號佇列（Pipeline 在每個 phase 開頭輪詢）"""
            cls._sync_signal_queue_from_legacy()
            drained = cls._signal_queue_svc.drain(signal_type)
            cls._signal_queue = cls._signal_queue_svc.queue
            return drained

        @classmethod
        def get_recent_events(cls, event_type: str='', limit: int=50) -> List[Dict[str, Any]]:
            """讀取最近的持久化事件（供 dashboard / 觀測）"""
            try:
                if cls._event_log_path and cls._log_store.event_log_path != cls._event_log_path:
                    cls._log_store.event_log_path = cls._event_log_path
                return cls._log_store.read_recent(event_type=event_type, limit=limit)
            except Exception:
                return []

        @classmethod
        def audit_event_contracts(cls, limit: int=100, *, fail_on_raw: bool=False, raw_policy: str | None=None) -> Dict[str, Any]:
            """Report raw-vs-semantic event usage during the migration window."""
            if raw_policy is None:
                raw_policy = 'block' if fail_on_raw else 'warn'
            if raw_policy not in {'allow', 'warn', 'block', 'strict'}:
                raise ValueError(f'unsupported raw event policy: {raw_policy}')
            events = cls.get_recent_events(limit=limit)
            semantic = []
            raw = []
            unknown = []
            for event in events:
                event_type = str(event.get('event_type') or '')
                if event_type in SEMANTIC_EVENT_TYPES:
                    semantic.append(event_type)
                elif event_type in RAW_EVENT_TYPES:
                    raw.append(event_type)
                else:
                    unknown.append(event_type)
            fail_raw = raw_policy in {'block', 'strict'}
            passed = not unknown and (not (fail_raw and raw))
            failure_reasons = []
            warning_reasons = []
            if unknown:
                failure_reasons.append('unknown_event_types_present')
            if fail_raw and raw:
                failure_reasons.append('raw_event_types_present')
            elif raw_policy == 'warn' and raw:
                warning_reasons.append('raw_event_types_present')
            return {'schema_version': 'nexus_event_contract_audit.v1', 'events_scanned': len(events), 'semantic_event_count': len(semantic), 'raw_event_count': len(raw), 'unknown_event_count': len(unknown), 'semantic_event_types': sorted(set(semantic)), 'raw_event_types': sorted(set(raw)), 'unknown_event_types': sorted(set(unknown)), 'transition_status': 'raw_events_present' if raw else 'semantic_only', 'strict_raw_mode': bool(fail_raw), 'raw_policy': raw_policy, 'warning_reasons': warning_reasons, 'failure_reasons': failure_reasons, 'passed': passed}
    values = {k: v for k, v in locals().items() if not k.startswith('__') and k not in {'bindings', '_nexus_generated_bindings'}}
    return TransportExports(values)
