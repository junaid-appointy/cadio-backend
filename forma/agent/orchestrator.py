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
            "name": "build_from_image",
            "description": (
                "Trace an attached flat image (logo, icon, badge, sign, silhouette) "
                "and build a 3D model of it by extruding the ACTUAL traced outline "
                "raised on a backing plate. Use this for any flat graphic instead of "
                "hand-coding shapes — hand-coding drops parts of the outline. The "
                "result reproduces the whole logo (every part), and is param-tweakable."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "asset_id": {"type": "string", "description": "id of the attached image"},
                    "width_mm": {"type": "number", "description": "overall width (default 40)"},
                    "logo_height_mm": {"type": "number", "description": "raised relief height (default 2)"},
                    "base_thickness_mm": {"type": "number", "description": "backing plate thickness, 0 for logo-only (default 1.5)"},
                    "label": {"type": "string"},
                },
                "required": ["asset_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_geometry",
            "description": (
                "Measure an uploaded reference solid (STEP/STL) the user attached. "
                "Returns its bounding box, volume, and (for STEP) cylindrical "
                "hole/boss diameters — real numbers to build a matching or mating "
                "part from ('make a lid for this'). Call it before asking the user "
                "for dimensions that the reference already defines."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "asset_id": {"type": "string", "description": "id of the attached geometry asset"},
                },
                "required": ["asset_id"],
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
        on_message: Callable[[str, dict], None] | None = None,
        inspect_asset: Callable[[str], dict] | None = None,
        trace_asset: Callable[[str, dict], Any] | None = None,
    ):
        self.model = model or DEFAULT_MODEL
        self.api_key = api_key or None  # falls back to provider env vars
        self.engine = engine
        self.runs_root = Path(runs_root)
        self.ask_user = ask_user or _default_ask_user
        self.inspect_asset = inspect_asset
        self.trace_asset = trace_asset
        self.on_event = on_event
        # on_message(role, record): fired as each message is appended, in a
        # base64-free persistence shape (see forma/api/history.py). Lets the
        # shell write conversation history to the DB in real time.
        self.on_message = on_message
        self.messages: list[dict[str, Any]] = []
        self.last_run_dir: Path | None = None
        self._turn_renders: list[Path] = []  # renders from the current turn's builds
        self._stop = threading.Event()

    def _supports_vision(self) -> bool:
        try:
            return bool(litellm.supports_vision(model=self.model))
        except Exception:
            return False

    def set_history(self, messages: list[dict[str, Any]]) -> None:
        """Restore prior conversation (LLM format) so a resumed session
        genuinely remembers. Does not re-fire on_message."""
        self.messages = list(messages)

    def _persist(self, role: str, record: dict) -> None:
        if self.on_message:
            try:
                self.on_message(role, record)
            except Exception:
                pass  # persistence must never break the agent loop

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

    def _run_program(self, code: str, params: dict | None, label: str) -> str:
        """Execute a build123d program, emit the run event, queue renders for
        the agent's eyes, and return the measured facts as a JSON string."""
        self._emit({"type": "status", "state": "running_cad", "label": label})
        run_dir = self.runs_root / datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        result = self.engine.execute(code, params, run_dir)
        self.last_run_dir = run_dir
        self._emit({"type": "run", "run_id": run_dir.name, "label": label,
                    "result": result.to_dict()})
        if result.ok and result.renders:
            self._turn_renders = [Path(p) for p in result.renders.values()]
        payload = result.to_dict()
        payload.pop("run_dir", None)
        payload.pop("renders", None)
        if result.ok:
            payload["artifacts"] = sorted(result.artifacts)
        return json.dumps(payload)

    def _handle_tool(self, name: str, tool_input: dict) -> str:
        if name == "ask_user":
            return json.dumps(self.ask_user(tool_input["questions"]))
        if name == "inspect_geometry":
            if not self.inspect_asset:
                return json.dumps({"error": "geometry inspection unavailable"})
            return json.dumps(self.inspect_asset(tool_input.get("asset_id", "")))
        if name == "run_cad":
            return self._run_program(
                tool_input["code"], tool_input.get("params"), tool_input.get("label", ""))
        if name == "build_from_image":
            if not self.trace_asset:
                return json.dumps({"error": "image tracing unavailable"})
            traced = self.trace_asset(tool_input.get("asset_id", ""), {
                "width_mm": tool_input.get("width_mm", 40.0),
                "logo_height_mm": tool_input.get("logo_height_mm", 2.0),
                "base_thickness_mm": tool_input.get("base_thickness_mm", 1.5),
            })
            if isinstance(traced, dict):  # an error
                return json.dumps(traced)
            return self._run_program(traced, None, tool_input.get("label", "from image"))
        return json.dumps({"error": f"unknown tool {name}"})

    @staticmethod
    def _image_block(path: Path) -> dict:
        mime = mimetypes.guess_type(str(path))[0] or "image/png"
        data = base64.standard_b64encode(path.read_bytes()).decode()
        # OpenAI image format — LiteLLM converts per provider (needs a
        # vision-capable model)
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}

    def send(self, user_message: str, images: list[dict] | None = None) -> str:
        """One conversational turn: returns the agent's final text.
        `images` are reference photos as {"id","path"} — shape/topology only;
        the corpus rules forbid the agent from reading dimensions off them."""
        if images:
            content: Any = [{"type": "text", "text": user_message}]
            content += [self._image_block(Path(img["path"])) for img in images]
        else:
            content = user_message
        self.messages.append({"role": "user", "content": content})
        # persisted base64-free: text + asset ids (re-embedded on replay)
        self._persist("user", {"text": user_message,
                               "image_asset_ids": [img["id"] for img in (images or [])]})
        self._turn_renders = []
        self._stop.clear()
        while True:
            if self._stop.is_set():
                return "⏹ Stopped."
            extra = {"api_key": self.api_key} if self.api_key else {}
            # push the model to reason harder about detailed multi-feature parts.
            # reasoning_effort is best-effort — drop_params silently removes it
            # for models that don't support it. (max_tokens kept at a value all
            # common models accept; the CAD program itself is small.)
            response = litellm.completion(
                model=self.model,
                max_tokens=16000,
                reasoning_effort="high",
                drop_params=True,
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
            self._persist("assistant", {"content": assistant["content"],
                                        "tool_calls": assistant.get("tool_calls")})

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
                self._persist("tool", {"tool_call_id": tc.id, "content": output})

            # Show the agent what it just built. Tool messages are text-only in
            # the OpenAI schema, so renders ride in a following user message.
            # NOT persisted: transient in-turn guidance; the agent's resulting
            # critique/fix (assistant messages) is what's worth replaying.
            if self._turn_renders and self._supports_vision():
                blocks: list[Any] = [{
                    "type": "text",
                    "text": ("Renders of the model you just built (iso / front / top / right, "
                             "plus a cut-away SECTION exposing the interior). Sharp feature "
                             "edges — holes, cuts, grooves, corners — are outlined in dark. Now "
                             "check TWO things: (1) COMPLETENESS — go through your feature "
                             "checklist; is every required feature present (every hole, cutout, "
                             "port, boss, rib, lip, fillet, text, mounting point)? (2) CORRECTNESS "
                             "— is every feature in the RIGHT PLACE, and are there any stray, "
                             "duplicate, or misplaced cuts/holes that should NOT be there? If "
                             "anything is missing, wrong, misplaced, or spurious, fix the "
                             "coordinates and rebuild. Only reply to the user once the model is "
                             "both complete and clean."),
                }]
                for p in self._turn_renders:
                    try:
                        blocks.append(self._image_block(p))
                    except OSError:
                        pass
                self.messages.append({"role": "user", "content": blocks})
            self._turn_renders = []
