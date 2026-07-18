"""The agent loop: any LLM + tools -> validated parametric models.

Provider-agnostic via LiteLLM: the same loop runs on Claude, GPT, Gemini,
Grok, etc. Pick the model with CADIO_MODEL (or --model on the CLI); API keys
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
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import litellm

from .corpus import system_corpus
from ..engines.base import Engine

DEFAULT_MODEL = os.environ.get("CADIO_MODEL", "anthropic/claude-opus-4-8")

# Output-token budget per LLM call. The old 16k cap truncated the model mid-turn
# on detailed parts (long program + high-effort reasoning), so it never reached
# the run_cad tool call and the half-written program leaked into the chat as the
# "answer". 32k comfortably fits a full program plus reasoning on every common
# provider (Claude/GPT/Gemini all accept it). Override with CADIO_MAX_OUTPUT_TOKENS.
MAX_OUTPUT_TOKENS = int(os.environ.get("CADIO_MAX_OUTPUT_TOKENS", "32000"))
# How many times to re-prompt after a truncated, tool-less response before giving
# up with a real message (never raw code) — guards against an infinite retry loop.
MAX_TRUNCATION_RETRIES = 2
# Builds allowed in ONE turn before the agent must stop and talk to the user.
# A weak model that can't fix a validation error will otherwise rebuild forever
# (observed: 9 invalid versions in a single request) while the user stares at
# "building model…". Override with CADIO_MAX_BUILDS_PER_TURN.
MAX_BUILDS_PER_TURN = int(os.environ.get("CADIO_MAX_BUILDS_PER_TURN", "5"))

# validation code -> one plain sentence the USER sees in the live status line
# while the agent reworks the model. Keep these human: no jargon, no traceback.
_FRIENDLY_FAILURES = {
    "not_watertight": "the model has gaps (parts only touching, not overlapping)",
    "winding": "some surfaces came out inside-out",
    "non_positive_volume": "the build produced no usable solid",
    "empty_mesh": "the build produced no geometry",
    "stl_unreadable": "the exported model couldn't be read back",
    "bbox_mismatch": "the built size doesn't match the intended dimensions",
}


# validation code -> targeted corrective, injected into the tool result when the
# SAME error fails twice in a row (the model is thrashing; give it the recipe).
_ESCALATIONS = {
    "not_watertight": (
        "You have failed watertightness twice with the same approach. OVERLAP every "
        "pair of joined solids by at least 0.2mm — never let solids merely touch at a "
        "face, edge, or point. Check every Pos() offset against the neighbour's extent."),
    "bbox_mismatch": (
        "The measured bounding box disagreed with the BREP twice. A feature is "
        "protruding outside the intended envelope, or a dimension is on the wrong "
        "axis. Re-derive each feature's position from the parameters on paper first."),
    "winding": (
        "Surface orientation failed twice. Avoid mirror()/negative scaling; build the "
        "mirrored part explicitly with its own positive geometry."),
    "program_error": (
        "The program errored twice. Fix ONLY the first error in the traceback; do not "
        "restructure anything else."),
}
_SIMPLIFY = (
    "STOP iterating on the full model — the same problem has now failed three times. "
    "Rebuild the SIMPLEST possible core (the one primary solid, no features) and make "
    "sure it validates clean. Then re-add features in SMALL batches, one build per "
    "batch, so the first failing feature is obvious.")


def _friendly_failure(result) -> str:
    """One short, user-facing reason why a build attempt isn't good yet."""
    if not result.ok:
        err = (result.error or "").strip()
        # asserts carry the agent's own requirement text — the most useful line
        if "AssertionError" in err:
            tail = err.rsplit("AssertionError", 1)[-1].strip(" :\n")
            if tail:
                return f"a requirement check failed: {tail.splitlines()[0][:120]}"
        if "busy" in err:
            return "the build queue is busy"
        return "the program errored while building"
    report = result.validation
    if report and not report.ok:
        for issue in report.issues:
            if issue.severity == "error" and issue.code in _FRIENDLY_FAILURES:
                return _FRIENDLY_FAILURES[issue.code]
        for issue in report.issues:
            if issue.severity == "error":
                return issue.message[:120]
    return "the result needs adjustments"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_cad",
            "description": (
                "Execute a precision-engine program (build123d Python, per the program "
                "contract) in the sandbox. Returns measured bbox/volume and the "
                "validation report. Use this to build and to self-check; fix any "
                "validation errors before answering the user. ALWAYS put the complete "
                "program in the `code` argument — NEVER paste the program (PARAMS/build) "
                "into your text reply. Building a model means calling this tool, not "
                "printing code; your text is only a short explanation."
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
        # base64-free persistence shape (see cadio/api/history.py). Lets the
        # shell write conversation history to the DB in real time.
        self.on_message = on_message
        self.messages: list[dict[str, Any]] = []
        self.last_run_dir: Path | None = None
        # id + source of the agent's last build, so the API can tell when the
        # user is looking at a DIFFERENT version than the agent last made (a
        # manual save, an older version) and anchor the next edit to it. Restored
        # from history on reconnect (see ProjectSession).
        self.last_run_id: str | None = None
        self.last_program: str | None = None
        self.last_usage: dict[str, Any] | None = None  # token/cost/time of the last turn
        self._turn_renders: list[Path] = []  # renders from the current turn's builds
        self._stop = threading.Event()
        self._turn_builds = 0                # run_cad calls in the current turn
        self._last_failure_code: str | None = None  # for repeat-failure escalation
        self._failure_streak = 0
        self._budget_refusals = 0            # refusals issued after the budget spent

    def _supports_vision(self) -> bool:
        # cached per model: consulted on every build now (it decides whether the
        # engine renders all agent-eye views or just the thumbnail)
        cache = getattr(self, "_vision_cache", None)
        if cache is not None and cache[0] == self.model:
            return cache[1]
        try:
            supported = bool(litellm.supports_vision(model=self.model))
        except Exception:
            supported = False
        self._vision_cache = (self.model, supported)
        return supported

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
        # static (corpus + contract + rules don't change within a session), so
        # build the string once instead of concatenating the whole corpus on
        # every LLM call in the tool loop.
        cached = getattr(self, "_system_prompt_cache", None)
        if cached is not None:
            return cached
        prompt = (
            "You are CADIO, an AI agent that turns user intent into accurate, "
            "parametric, exportable 3D models. You never click a GUI — you write "
            "programs that the engine executes, then you inspect the measured "
            "results and iterate until the validation report is clean and the "
            "geometry matches the requirements.\n\n"
            "HOW YOU BUILD: to make a model you CALL the run_cad tool with the "
            "complete program in its `code` argument. NEVER write the program "
            "(PARAMS, build(), features()) into your text reply — the user wants a "
            "built model, not source code. Do your planning briefly, then call the "
            "tool. Your text messages are only short explanations, never the program.\n\n"
            "When a message says the user SELECTED a specific part/feature of the "
            "current model, scope your edit to THAT feature and preserve everything "
            "else. Define a features() map naming the parts users are likely to "
            "point at (one named construction sub-solid per part, never positional "
            "bands), and keep those names stable across rebuilds. If a selection "
            "note says the selected part has NO controlling parameter, add one for "
            "it in the same edit so the user can tweak it afterward.\n\n"
            "ENGINE PROGRAM CONTRACT:\n" + self.engine.program_contract() + "\n\n"
            + system_corpus()
        )
        self._system_prompt_cache = prompt
        return prompt

    def _run_program(self, code: str, params: dict | None, label: str) -> str:
        """Execute a build123d program, emit the run event, queue renders for
        the agent's eyes, and return the measured facts as a JSON string.

        Narrates the attempt over the status channel: which attempt this is
        (n / budget) and, when a build isn't clean, a one-line human reason —
        so the user watches progress instead of a silent multi-minute spinner."""
        self._turn_builds += 1
        n = self._turn_builds
        self._emit({"type": "status", "state": "running_cad", "label": label,
                    "attempt": n, "max_attempts": MAX_BUILDS_PER_TURN})
        run_dir = self.runs_root / datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        # renders: the full agent-eye set only when a vision model will actually
        # look at them; otherwise just the iso thumbnail (~1/5 the render CPU).
        # on_wait: when the box is saturated the user sees an honest queue
        # position instead of a silently longer "building…" spinner. It fires
        # UNDER the exec-gate lock, and delivery can block seconds on a dead
        # socket — so the emit is dispatched on a throwaway thread.
        def _queued(ahead: int) -> None:
            threading.Thread(
                target=self._emit, daemon=True,
                args=({"type": "status", "state": "queued", "label": label,
                       "position": ahead, "attempt": n,
                       "max_attempts": MAX_BUILDS_PER_TURN},),
            ).start()

        result = self.engine.execute(
            code, params, run_dir,
            renders="full" if self._supports_vision() else "thumbnail",
            on_wait=_queued,
        )
        self.last_run_dir = run_dir
        self.last_run_id = run_dir.name
        self.last_program = code
        self._emit({"type": "run", "run_id": run_dir.name, "label": label,
                    "result": result.to_dict()})
        clean = bool(result.ok and (result.validation is None or result.validation.ok))
        if not clean:
            self._emit({"type": "status", "state": "fixing", "attempt": n,
                        "max_attempts": MAX_BUILDS_PER_TURN,
                        "detail": _friendly_failure(result)})
        self._track_failure(result, clean)
        if result.ok and result.renders:
            self._turn_renders = [Path(p) for p in result.renders.values()]
        payload = result.to_dict()
        payload.pop("run_dir", None)
        payload.pop("renders", None)
        # profiling telemetry is for run meta / healthz, never for the LLM —
        # it's context-window noise the model can do nothing with
        payload.pop("timings", None)
        payload.pop("rss_peak_mb", None)
        payload.pop("stl_facets", None)
        if result.ok:
            payload["artifacts"] = sorted(result.artifacts)
        payload["attempts_used"] = f"{n}/{MAX_BUILDS_PER_TURN}"
        # repeat-failure escalation: same error twice -> the targeted recipe;
        # three times -> force the simplify-and-stage strategy. Rides in the
        # tool result so it lands exactly when the model plans its next build.
        if self._failure_streak >= 3:
            payload["guidance"] = _SIMPLIFY
        elif self._failure_streak == 2:
            payload["guidance"] = _ESCALATIONS.get(
                self._last_failure_code or "",
                "The same error failed twice — change approach, do not retry the "
                "same fix a third time.")
        return json.dumps(payload)

    def _track_failure(self, result, clean: bool) -> None:
        """Maintain the consecutive-identical-failure streak for escalation."""
        if clean:
            self._last_failure_code, self._failure_streak = None, 0
            return
        code = "program_error"
        if result.ok and result.validation and not result.validation.ok:
            code = next((i.code for i in result.validation.issues if i.severity == "error"),
                        "validation_error")
        if code == self._last_failure_code:
            self._failure_streak += 1
        else:
            self._last_failure_code, self._failure_streak = code, 1

    def _over_budget(self) -> str | None:
        """A refusal payload once the per-turn build budget is spent, else None.
        The refusal instructs the model to wrap up honestly instead of building."""
        if self._turn_builds < MAX_BUILDS_PER_TURN:
            return None
        self._budget_refusals += 1
        return json.dumps({
            "error": f"build budget for this turn is exhausted ({MAX_BUILDS_PER_TURN} builds)",
            "instruction": (
                "Do NOT build again this turn. Reply to the user now with: (1) what "
                "you built and what state the latest version is in, (2) the one issue "
                "you could not resolve, in plain words, and (3) a concrete suggestion "
                "or question so THEY choose how to proceed. Be honest and brief."),
        })

    def _handle_tool(self, name: str, tool_input: dict) -> str:
        if name == "ask_user":
            return json.dumps(self.ask_user(tool_input["questions"]))
        if name == "inspect_geometry":
            if not self.inspect_asset:
                return json.dumps({"error": "geometry inspection unavailable"})
            return json.dumps(self.inspect_asset(tool_input.get("asset_id", "")))
        if name == "run_cad":
            refusal = self._over_budget()
            if refusal:
                return refusal
            return self._run_program(
                tool_input["code"], tool_input.get("params"), tool_input.get("label", ""))
        if name == "build_from_image":
            refusal = self._over_budget()
            if refusal:
                return refusal
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
        self._turn_builds = 0
        self._last_failure_code, self._failure_streak = None, 0
        self._budget_refusals = 0
        # per-turn accounting: a turn may make several LLM calls (tool loop)
        t0 = time.monotonic()
        tin = tout = calls = 0
        cost = 0.0
        cost_ok = True
        truncations = 0  # consecutive length-capped replies with no tool call

        def turn_usage() -> dict[str, Any]:
            return {
                "input_tokens": tin,
                "output_tokens": tout,
                "llm_calls": calls,
                "cost_usd": round(cost, 4) if cost_ok else None,
                "duration_s": round(time.monotonic() - t0, 1),
                "model": self.model,
            }

        while True:
            if self._stop.is_set():
                if calls:
                    self.last_usage = turn_usage()
                    self._emit({"type": "usage", "usage": self.last_usage})
                return "⏹ Stopped."
            # hard stop: budget spent AND the model ignored two refusals — end the
            # turn ourselves rather than let it burn LLM calls asking to build.
            if self._budget_refusals >= 2:
                self.last_usage = turn_usage()
                reply = ("I hit this turn's build limit without getting a clean "
                         "result. The latest version is saved — tell me whether to "
                         "keep refining it, simplify the design, or try a different "
                         "approach, and I'll continue.")
                self._persist("assistant", {"content": reply, "tool_calls": None,
                                            "usage": self.last_usage})
                self._emit({"type": "usage", "usage": self.last_usage})
                return reply
            extra = {"api_key": self.api_key} if self.api_key else {}
            # push the model to reason harder about detailed multi-feature parts.
            # reasoning_effort is best-effort — drop_params silently removes it
            # for models that don't support it. max_tokens is generous so a full
            # program + reasoning fits in one call (a tight cap used to truncate the
            # turn before the run_cad tool call — see the truncation guard below).
            response = litellm.completion(
                model=self.model,
                max_tokens=MAX_OUTPUT_TOKENS,
                reasoning_effort="high",
                drop_params=True,
                messages=[{"role": "system", "content": self._system_prompt()}]
                + self.messages,
                tools=TOOLS,
                **extra,
            )
            calls += 1
            u = getattr(response, "usage", None)
            if u:
                tin += int(getattr(u, "prompt_tokens", 0) or 0)
                tout += int(getattr(u, "completion_tokens", 0) or 0)
            try:
                cost += litellm.completion_cost(completion_response=response)
            except Exception:
                cost_ok = False  # unknown/self-hosted model — hide the $ figure

            choice = response.choices[0]
            msg = choice.message
            tool_calls = msg.tool_calls or []

            # Truncation guard: the model hit the output cap and stopped WITHOUT
            # calling a tool — almost always because it was writing the program
            # into its reply and ran out of room. Never surface that half-written
            # code as the answer. Nudge it to call run_cad and retry (bounded), and
            # if it still won't, return a real message instead of the code dump.
            if getattr(choice, "finish_reason", None) in ("length", "max_tokens") and not tool_calls:
                truncations += 1
                if truncations > MAX_TRUNCATION_RETRIES:
                    self.last_usage = turn_usage()
                    reply = ("I couldn't fit that build into a single step — the program kept "
                             "getting cut off before it ran. Try again, or ask for it in a few "
                             "smaller steps (fewer features at a time).")
                    self._persist("assistant", {"content": reply, "tool_calls": None,
                                                 "usage": self.last_usage})
                    self._emit({"type": "usage", "usage": self.last_usage})
                    return reply
                # keep the partial in the model's working memory (NOT persisted, so
                # it never appears in the user's scrollback), then correct course.
                self.messages.append({"role": "assistant", "content": msg.content or ""})
                self.messages.append({"role": "user", "content": (
                    "Your previous message was cut off before you built anything, and you did "
                    "not call any tool. Do NOT paste the program into your reply. Call the "
                    "run_cad tool now with the COMPLETE program in its `code` argument; keep any "
                    "text to one short sentence.")})
                continue
            truncations = 0  # a complete (or tool-calling) response — reset the guard

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
            record: dict[str, Any] = {"content": assistant["content"],
                                      "tool_calls": assistant.get("tool_calls")}
            final = not tool_calls
            if final:
                # attach usage to the final assistant message so history replay
                # keeps it, and emit it live (before returning) for the UI
                self.last_usage = turn_usage()
                record["usage"] = self.last_usage
            self._persist("assistant", record)

            if final:
                self._emit({"type": "usage", "usage": self.last_usage})
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
