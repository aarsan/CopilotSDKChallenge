"""
InfraForge Pipeline Handlers — DB-driven workflow implementations.

Each sub-module registers step handlers on a ``PipelineRunner`` instance
and exports a ``runner`` object that web.py endpoints delegate to.

Modules
-------
- ``onboarding`` — Service onboarding (ARM generation → validation → promotion)
- ``validation`` — Template validation (deploy → heal → promote)
- ``deploy``     — Template deployment (sanitise → what-if → deploy → heal)
"""
