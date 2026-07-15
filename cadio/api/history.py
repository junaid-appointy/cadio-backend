"""One serializer, two consumers.

Stored message records (base64-free, in the DB) convert to:
  - LLM-replay format  (re-embeds reference images as base64 from asset files)
  - UI scrollback items (the frontend ChatItem shapes)

Keeping both derivations here guarantees the agent's memory and the user's
scrollback never drift apart on resume.

Stored record shapes (role -> content JSON):
  user      {"text": str, "image_asset_ids": [id, ...]}
  assistant {"content": str, "tool_calls": [...], "usage": {...}?}  # usage on the final msg
  tool      {"tool_call_id": str, "content": str}          # LLM-native
  event     {"kind": "run"|"checkpoint", "run_id": str}    # UI-only marker
              # run = agent build; checkpoint = manual save (Params/Code tab)
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any, Callable


def _image_block(path: Path) -> dict:
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    data = base64.standard_b64encode(path.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


def _window(records: list[dict], max_turns: int | None) -> list[dict]:
    """Keep only the last `max_turns` conversation turns (a turn starts at a
    `user` record). Slicing on a user boundary keeps each turn whole, so every
    assistant tool_call still has its matching tool result — trimming elsewhere
    would desync the pairing and the provider would reject the request. Bounds
    the token cost + base64 image re-embedding of resuming a long conversation."""
    if not max_turns or max_turns <= 0:
        return records
    starts = [i for i, r in enumerate(records) if r["role"] == "user"]
    if len(starts) <= max_turns:
        return records
    return records[starts[-max_turns]:]


def to_llm_messages(records: list[dict], asset_path: Callable[[str], Path | None],
                    max_turns: int | None = None) -> list[dict[str, Any]]:
    """Rebuild the exact message list the orchestrator keeps in memory, optionally
    windowed to the last `max_turns` turns."""
    out: list[dict[str, Any]] = []
    for rec in _window(records, max_turns):
        role, content = rec["role"], rec["content"]
        if role == "user":
            ids = content.get("image_asset_ids") or []
            paths = [p for aid in ids if (p := asset_path(aid))]
            if paths:
                blocks: list[Any] = [{"type": "text", "text": content.get("text", "")}]
                blocks += [_image_block(p) for p in paths]
                out.append({"role": "user", "content": blocks})
            else:
                out.append({"role": "user", "content": content.get("text", "")})
        elif role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": content.get("content", "")}
            if content.get("tool_calls"):
                msg["tool_calls"] = content["tool_calls"]
            out.append(msg)
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": content["tool_call_id"],
                        "content": content["content"]})
        # 'event' records are UI-only; the run result already reached the LLM
        # via the corresponding tool message.
    return out


def to_ui_items(records: list[dict], run_lookup: Callable[[str], dict | None],
                asset_lookup: Callable[[str], dict | None]) -> list[dict]:
    """Build ChatItem-shaped payloads for scrollback. Mirrors what the live
    websocket pushes: user bubbles, agent bubbles, run cards."""
    items: list[dict] = []
    for rec in records:
        role, content = rec["role"], rec["content"]
        if role == "user":
            images = [a for aid in (content.get("image_asset_ids") or [])
                      if (a := asset_lookup(aid))]
            items.append({"kind": "user", "text": content.get("text", ""),
                          "images": images or None})
        elif role == "assistant":
            text = (content.get("content") or "").strip()
            if text:
                item = {"kind": "agent", "text": text}
                if content.get("usage"):  # token/cost/time attached to the final message
                    item["usage"] = content["usage"]
                items.append(item)
        elif role == "event" and content.get("kind") in ("run", "checkpoint"):
            # 'run' = an agent build (a full run card); 'checkpoint' = a manual
            # save (a compact chip). Both point at a run's meta by id.
            meta = run_lookup(content["run_id"])
            if meta:
                kind = "run" if content["kind"] == "run" else "checkpoint"
                items.append({"kind": kind, "meta": meta})
    return items
