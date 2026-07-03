"""The agent loop: any LLM + tools -> validated parametric models.

Provider-agnostic via LiteLLM: the same loop runs on Claude, GPT, Gemini,
Grok, etc. Pick the model with FORMA_MODEL (or --model on the CLI); API keys
come from each provider's standard env var:

    anthropic/claude-opus-4-8      ANTHROPIC_API_KEY   (default)
    openai/gpt-5.2                 OPENAI_API_KEY
    gemini/gemini-3-pro            GEMINI_API_KEY
    xai/grok-4                     XAI_API_KEY

Manual tool loop (OpenAI wire format, which LiteLLM normalizes every provider
to) because the shell owns it: every run is persisted as a version, and the
web UI receives live events (`on_event`) over a websocket.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import litellm

from .corpus import system_corpus
from ..engines.base import Engine

DEFAULT_MODEL = os.environ.get("FORMA_MODEL", "anthropic/claude-opus-4-8")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_cad",
            "description": (
                "Execute a precision-engine program (build123d Python, per the program "
                "contract) in the sandbox. Returns measured bbox/volume and the "
                "validation report. Use this to build and to self-check; fix any "
                "validation errors before answering the user."
            ),
            "parameters": {
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
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "Ask the user clarifying questions (max 4 per call), each with a "
                "suggested default. Use for dimensions, tolerances, materials — "
                "anything the requirements need. Never guess numbers from photos."
            ),
            "parameters": {
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
        model: str | None = None,
        api_key: str | None = None,
        ask_user: Callable[[list[dict]], list[dict]] | None = None,
        on_event: Callable[[dict], None] | None = None,
    ):
        self.model = model or DEFAULT_MODEL
        self.api_key = api_key or None  # falls back to provider env vars
        self.engine = engine
        self.runs_root = Path(runs_root)
        self.ask_user = ask_user or _default_ask_user
        self.on_event = on_event
        self.messages: list[dict[str, Any]] = []
        self.last_run_dir: Path | None = None
        self._stop = threading.Event()

    def request_stop(self) -> None:
        """Cooperative cancel: takes effect between LLM calls / tool runs.
        Pending tool calls get a 'cancelled' result so history stays valid."""
        self._stop.set()

    def _emit(self, event: dict) -> None:
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                pass  # UI notification must never break the loop

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
            label = tool_input.get("label", "")
            self._emit({"type": "status", "state": "running_cad", "label": label})
            run_dir = self.runs_root / datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
            result = self.engine.execute(
                tool_input["code"], tool_input.get("params"), run_dir
            )
            self.last_run_dir = run_dir
            self._emit(
                {
                    "type": "run",
                    "run_id": run_dir.name,
                    "label": label,
                    "result": result.to_dict(),
                }
            )
            payload = result.to_dict()
            # the agent needs facts + verdict, not file paths
            payload.pop("run_dir", None)
            if result.ok:
                payload["artifacts"] = sorted(result.artifacts)
            return json.dumps(payload)
        return json.dumps({"error": f"unknown tool {name}"})

    @staticmethod
    def _image_block(path: Path) -> dict:
        mime = mimetypes.guess_type(str(path))[0] or "image/png"
        data = base64.standard_b64encode(path.read_bytes()).decode()
        # OpenAI image format — LiteLLM converts per provider (needs a
        # vision-capable model)
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}

    def send(self, user_message: str, images: list[Path] | None = None) -> str:
        """One conversational turn: returns the agent's final text.
        `images` are reference photos — shape/topology only; the corpus rules
        forbid the agent from reading dimensions off them."""
        if images:
            content: Any = [{"type": "text", "text": user_message}]
            content += [self._image_block(Path(p)) for p in images]
        else:
            content = user_message
        self.messages.append({"role": "user", "content": content})
        self._stop.clear()
        while True:
            if self._stop.is_set():
                return "⏹ Stopped."
            extra = {"api_key": self.api_key} if self.api_key else {}
            response = litellm.completion(
                model=self.model,
                max_tokens=16000,
                messages=[{"role": "system", "content": self._system_prompt()}]
                + self.messages,
                tools=TOOLS,
                **extra,
            )
            msg = response.choices[0].message
            tool_calls = msg.tool_calls or []

            assistant: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
            if tool_calls:
                assistant["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ]
            self.messages.append(assistant)

            if not tool_calls:
                return msg.content or ""

            for tc in tool_calls:
                if self._stop.is_set():
                    output = json.dumps({"cancelled": "the user stopped this turn"})
                else:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError as exc:
                        output = json.dumps({"error": f"invalid tool arguments: {exc}"})
                    else:
                        output = self._handle_tool(tc.function.name, args)
                self.messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": output}
                )
