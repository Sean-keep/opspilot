"""Task orchestration: the API side of the task engine.

The worker owns execution; this package owns the HTTP surface — submitting
work, looking at it, stopping it, and ruling on runs the recovery pass could
not classify. Models live in :mod:`shared.task_protocol` so both processes
agree on the schema without importing each other.
"""
