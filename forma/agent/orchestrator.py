"""The agent loop: Claude + tools -> validated parametric models.

Manual tool-use loop (not the SDK tool runner) because the shell needs to own
the loop: it persists every run as a version, and P1 will stream events to the
web UI over a websocket.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

import anthropic

from .corpus import system_corpus
from ..engines.base import Engine

MODEL = "claude-opus-4-8"

TOOLS = [
    {
        "name": "run_cad",
        "description": (
            "Execute a precision-engine program (build123d Python, per the program "
            "contract) in the sandbox. Returns measured bbox/volume and the "
            "validation report. Use this to build and to self-check; fix any "
            "validation errors before answering the user."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Complete program file content"},
                "params": {
                    "type": "object",
                    "description": "Optional parameter overrides (name -> value)",
                },
                "label": {"type": "string", "description": "Short label for this version"},
            },
            "required": ["code"],
        },
    },
    {
        "name": "ask_user",
        "description": (
            "Ask the user clarifying questions (max 4 per call), each with a "
            "suggested default. Use for dimensions, tolerances, materials — "
            "anything the requirements need. Never guess numbers from photos."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string"},
                            "default": {"type": "string"},
                        },
                        "required": ["question"],
                    },
                }
            },
            "required": ["questions"],
        },
    },
]


def _default_ask_user(questions: list[dict]) -> list[dict]:
    """CLI implementation of ask_user: prompt on the terminal."""
    answers = []
    for q in questions:
        default = q.get("default", "")
        suffix = f" [{default}]" if default else ""
        try:
            raw = input(f"? {q['question']}{suffix}: ").strip()
        except EOFError:
            raw = ""
        answers.append({"question": q["question"], "answer": raw or default})
    return answers


class Orchestrator:
    def __init__(
        self,
        engine: Engine,
        runs_root: Path,
        ask_user: Callable[[list[dict]], list[dict]] | None = None,
    ):
        self.client = anthropic.Anthropic()
        self.engine = engine
        self.runs_root = Path(runs_root)
        self.ask_user = ask_user or _default_ask_user
        self.messages: list[dict[str, Any]] = []
        self.last_run_dir: Path | None = None

    def _system_prompt(self) -> str:
        return (
            "You are Forma, an AI agent that turns user intent into accurate, "
            "parametric, exportable 3D models. You never click a GUI — you write "
            "programs that the engine executes, then you inspect the measured "
            "results and iterate until the validation report is clean and the "
            "geometry matches the requirements.\n\n"
            "ENGINE PROGRAM CONTRACT:\n" + self.engine.program_contract() + "\n\n"
            + system_corpus()
        )

    def _handle_tool(self, name: str, tool_input: dict) -> str:
        if name == "ask_user":
            return json.dumps(self.ask_user(tool_input["questions"]))
        if name == "run_cad":
            run_dir = self.runs_root / time.strftime("%Y%m%d-%H%M%S")
            result = self.engine.execute(
                tool_input["code"], tool_input.get("params"), run_dir
            )
            self.last_run_dir = run_dir
            payload = result.to_dict()
            # the agent needs facts + verdict, not file paths
            payload.pop("run_dir", None)
            if result.ok:
                payload["artifacts"] = sorted(result.artifacts)
            return json.dumps(payload)
        return json.dumps({"error": f"unknown tool {name}"})

    def send(self, user_message: str) -> str:
        """One conversational turn: returns the agent's final text."""
        self.messages.append({"role": "user", "content": user_message})
        while True:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=16000,
                thinking={"type": "adaptive"},
                system=[{
                    "type": "text",
                    "text": self._system_prompt(),
                    "cache_control": {"type": "ephemeral"},
                }],
                tools=TOOLS,
                messages=self.messages,
            )
            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason != "tool_use":
                return next(
                    (b.text for b in response.content if b.type == "text"), ""
                )

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    output = self._handle_tool(block.name, block.input)
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": output}
                    )
            self.messages.append({"role": "user", "content": tool_results})
