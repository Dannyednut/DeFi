from .route_ir import ExecutionKind, ExecutionLeg, ExecutionPlan
from .arbexec_adapter import ArbExecRoute, build_arbexec_route
from .protocol_builders import ProtocolCall

__all__ = [
    'ArbExecRoute','build_arbexec_route','ExecutionKind','ExecutionLeg','ExecutionPlan','ProtocolCall','CallSpec','RouteCompiler','AtomicExecutionService','AtomicEnvelope','AtomicPreflight']

def __getattr__(name):
    if name in {'CallSpec','RouteCompiler'}:
        from .route_compiler import CallSpec, RouteCompiler
        return {'CallSpec': CallSpec, 'RouteCompiler': RouteCompiler}[name]
    if name in {'AtomicExecutionService','AtomicEnvelope','AtomicPreflight'}:
        from .atomic import AtomicExecutionService, AtomicEnvelope, AtomicPreflight
        return {'AtomicExecutionService': AtomicExecutionService, 'AtomicEnvelope': AtomicEnvelope, 'AtomicPreflight': AtomicPreflight}[name]
    raise AttributeError(name)
