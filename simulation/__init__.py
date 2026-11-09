"""Exact-route simulation (research-only surface).

ExactRouteSimulator quotes a route hop-by-hop through the protocol
adapters in protocols/registry.py. It has NO production caller: the live
path quotes candidates through the on-chain ArbExec.yieldOut() call in
detectors/base.py and validates them with OpportunityValidator
(validation/opportunity.py). See docs/DEPENDENCIES.md §11 for the
research-only disposition: compiling or simulating a protocol route does
NOT make that protocol executable through the deployed Exec.sol, which
executes Uniswap-V2-style pairs and Uniswap-V3 pools only.
"""
from .exact_route import ExactRouteSimulator, RouteSimulation, SimulationLegResult

__all__ = [
    'ExactRouteSimulator', 'RouteSimulation', 'SimulationLegResult',
]
