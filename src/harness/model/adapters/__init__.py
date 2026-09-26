"""Transport adapters implementing ``ModelClient``.

Only ``harness.model.factory`` imports from here; the core (planner, executor,
orchestrator, verification, recovery, context) depends on ``ModelClient`` and the
normalized types only.
"""
