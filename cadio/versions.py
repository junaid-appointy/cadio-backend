"""Version identity + the "current model" note that keeps the agent editing
what the user is actually looking at.

Every build is a version (a run). The agent only remembers the versions IT built
via run_cad; when the user saves a version by hand (a param tweak or a Code-tab
run) or scrolls back to an older one, the agent's in-memory program goes stale
and its next edit would silently discard that work. Before each turn the API
compares the on-screen version to the agent's last build and, when they differ,
injects the note below so the agent starts from the right program + parameters.

Pure string/dict helpers — no I/O, no LLM — so the API layer owns loading and
this stays trivially testable.
"""

from __future__ import annotations

from typing import Any


def version_name(run: dict) -> str:
    """Human name for a version: its label if set, else 'v<seq>' (a stable,
    monotonic per-project number stamped at save time), falling back to the raw
    run id for legacy runs written before seq existed."""
    label = (run.get("label") or "").strip()
    if label:
        return label
    seq = (run.get("meta") or {}).get("seq")
    return f"v{seq}" if seq else run.get("run_id", "?")


def _fmt_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _fmt_params(params: dict) -> str:
    return ", ".join(f"{k}={_fmt_value(v)}" for k, v in params.items())


def current_model_note(name: str, run_id: str, params: dict | None,
                       program: str | None) -> str:
    """The agent-facing note describing the version on screen. `program` is
    included only when it differs from what the agent last built (the user ran
    different code or loaded an older version); a param-only change omits it and
    just restates the values so the agent preserves them."""
    lines = [
        f"[CURRENT MODEL] The user is now looking at version “{name}” "
        f"(run {run_id}), which is NOT the model you last built. Treat THIS "
        "version as the current model: base any change on it and preserve "
        "everything the user hasn't asked to change."
    ]
    if params:
        lines.append(
            f"Its current parameter values are: {_fmt_params(params)}. Keep these "
            "values unless the user asks to change them."
        )
    if program:
        lines.append(
            "This version was built by exactly this program — start from it:\n"
            "```python\n" + program.strip() + "\n```"
        )
    return "\n\n".join(lines)
