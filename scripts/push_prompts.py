"""Push agent prompts to Langfuse Prompt Management on each deploy (QNT-199).

Reads the five system prompts from agent.prompts (git source of truth),
registers each as a new named version in Langfuse Prompt Management with the
``production`` label, and exits 0. Every subsequent trace links to the
exact version in use at deploy time via ``langfuse_prompt`` metadata injected
in graph.py.

Run via CD (deploy.yml) after the container health gate::

    docker exec equity-data-agent-api-1 \\
        /app/.venv/bin/python /app/scripts/push_prompts.py

Exits 0 when Langfuse keys are unset (CI / offline dev) -- the agent falls
back to content-hash metadata in that case.
"""

from __future__ import annotations

import sys

from shared.config import settings


def _load_prompts() -> dict[str, str]:
    from agent.prompts import (
        COMPARISON_SYSTEM_PROMPT,
        CONVERSATIONAL_SYSTEM_PROMPT,
        FOCUSED_SYSTEM_PROMPT,
        QUICK_FACT_SYSTEM_PROMPT,
        SYSTEM_PROMPT,
    )

    return {
        "system-prompt": SYSTEM_PROMPT,
        "quick-fact-prompt": QUICK_FACT_SYSTEM_PROMPT,
        "comparison-prompt": COMPARISON_SYSTEM_PROMPT,
        "conversational-prompt": CONVERSATIONAL_SYSTEM_PROMPT,
        "focused-prompt": FOCUSED_SYSTEM_PROMPT,
    }


def main() -> int:
    if not (settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY):
        print("LANGFUSE keys not set -- skipping prompt push (hash fallback active)")
        return 0

    from langfuse import Langfuse

    prompts = _load_prompts()
    client = Langfuse(
        public_key=settings.LANGFUSE_PUBLIC_KEY,
        secret_key=settings.LANGFUSE_SECRET_KEY,
        base_url=settings.LANGFUSE_BASE_URL,
    )

    pushed = 0
    for name, text in prompts.items():
        try:
            result = client.create_prompt(
                name=name,
                prompt=text,
                labels=["production"],
                type="text",
            )
            print(f"Pushed {name}: version {result.version}")
            pushed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR pushing {name}: {exc}", file=sys.stderr)

    client.flush()
    total = len(prompts)
    print(f"Done: {pushed}/{total} prompts pushed to Langfuse Prompt Management")
    return 0 if pushed == total else 1


if __name__ == "__main__":
    sys.exit(main())
