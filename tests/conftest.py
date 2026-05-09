"""Root pytest fixtures.

Disable Langfuse tracing for the whole test session. ``shared.config.Settings``
reads ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` from the workspace
``.env`` via pydantic-settings, which means a local pytest run with real keys
on disk (the developer's prod credentials) emits traces to production
Langfuse for every ``@observe``-decorated call exercised by a test —
``agent.__main__.analyze``, ``agent.tools.get_*_report``, the graph nodes,
and so on. The leak shows up as orphan single-span traces in the dashboard
because tests typically invoke these entry points outside any parent span.

Empty keys alone aren't enough: the langfuse SDK's ``@observe`` decorator
maintains its own global singleton that, with no keys, still POSTs spans to
``cloud.langfuse.com`` and only fails at the 401. ``LANGFUSE_TRACING_ENABLED``
is the SDK's official kill switch — set to ``false`` it short-circuits the
exporter entirely and no HTTP call is made.

Tests that explicitly need Langfuse-enabled behaviour
(``tests/agent/test_tracing.py::test_enabled_when_both_keys_present``)
already monkeypatch fresh settings + a stub SDK and construct their own
``LangfuseResource`` instance, so they're unaffected.
"""

from __future__ import annotations

import os

os.environ["LANGFUSE_PUBLIC_KEY"] = ""
os.environ["LANGFUSE_SECRET_KEY"] = ""
os.environ["LANGFUSE_TRACING_ENABLED"] = "false"
