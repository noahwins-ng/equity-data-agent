"""QNT-294 (AC1): per-node modules split out of graph.py.

Each graph node (classify, plan, gather, explore_supervisor, synthesize,
narrate, clarify) is a module-level function taking ``(state, config, deps)``
so it is unit-testable without compiling a graph. ``build_graph`` binds ``deps``
via ``functools.partial`` and wires the same topology as before.
"""
