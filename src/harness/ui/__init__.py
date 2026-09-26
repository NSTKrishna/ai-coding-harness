"""Presentation layer for the interactive CLI: terminal capability detection,
formatting, input collection and renderers. Consumes ``RunState``/telemetry
events only; never a source of agent state and never imported by
``harness.orchestrator``/``harness.verify``/``harness.tools``/``harness.model``.
"""
