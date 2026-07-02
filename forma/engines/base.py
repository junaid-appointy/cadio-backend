"""The engine contract that keeps the shell generic.

Every geometry engine (precision/build123d today; Blender, generative later)
implements `Engine`. The shell — agent, API, CLI, viewer — only ever talks to
this interface.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Protocol


@dataclass
class ParamSpec:
    """One user-tweakable parameter, extracted from a program's PARAMS list.

    This is what drives UI sliders — re-executing with new values must never
    require an LLM call.
    """

    name: str
    default: float | int | str | bool
    type: str = "number"  # number | integer | string | boolean
    min: float | None = None
    max: float | None = None
    unit: str = "mm"
    description: str = ""
    group: str = "General"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationIssue:
    severity: str  # "error" | "warning"
    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationReport:
    ok: bool
    issues: list[ValidationIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "issues": [i.to_dict() for i in self.issues]}


@dataclass
class ExecutionResult:
    """What comes back from running a program in an engine's sandbox."""

    ok: bool
    run_dir: Path
    params: dict[str, Any] = field(default_factory=dict)
    manifest: list[ParamSpec] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)  # kind -> file path
    bbox: dict[str, list[float]] | None = None  # {"min": [x,y,z], "max": [x,y,z], "size": [...]}
    volume_mm3: float | None = None
    validation: ValidationReport | None = None
    error: str | None = None  # traceback / message when ok is False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "run_dir": str(self.run_dir),
            "params": self.params,
            "manifest": [p.to_dict() for p in self.manifest],
            "artifacts": self.artifacts,
            "bbox": self.bbox,
            "volume_mm3": self.volume_mm3,
            "validation": self.validation.to_dict() if self.validation else None,
            "error": self.error,
        }


class Engine(Protocol):
    """Pluggable geometry backend. See ai-3d-product-plan.md §6.3."""

    id: str
    domains: list[str]

    def execute(
        self, code: str, params: dict[str, Any] | None, run_dir: Path
    ) -> ExecutionResult:
        """Run a program in the sandbox and export geometry + preview.

        Re-execution with new `params` must not require an LLM call.
        """
        ...

    def program_contract(self) -> str:
        """Human/LLM-readable description of the program format this engine
        executes (used in the agent's system prompt)."""
        ...
