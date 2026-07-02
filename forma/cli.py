"""Forma CLI — the Phase-0 harness.

  python -m forma.cli run examples/simple_box.py --set length=200 --set wall=3
  python -m forma.cli chat            # agent REPL (needs ANTHROPIC_API_KEY / ant auth)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .engines.precision import PrecisionEngine

RUNS_ROOT = Path.cwd() / "runs"


def cmd_run(args) -> int:
    code = Path(args.program).read_text()
    params = {}
    for pair in args.set or []:
        name, _, value = pair.partition("=")
        params[name] = value
    run_dir = Path(args.out) if args.out else RUNS_ROOT / time.strftime("%Y%m%d-%H%M%S")

    engine = PrecisionEngine()
    result = engine.execute(code, params, run_dir)

    if not result.ok:
        print("EXECUTION FAILED\n" + (result.error or ""), file=sys.stderr)
        return 1

    print(f"run dir : {result.run_dir}")
    print(f"params  : {json.dumps(result.params)}")
    size = result.bbox["size"]
    print(f"bbox    : {size[0]:.2f} x {size[1]:.2f} x {size[2]:.2f} mm")
    print(f"volume  : {result.volume_mm3 / 1000:.1f} cm^3")
    for kind, path in sorted(result.artifacts.items()):
        print(f"{kind:7s} : {path}")
    v = result.validation
    if v.ok:
        print("validation: OK")
    else:
        print("validation: FAILED")
        for issue in v.issues:
            print(f"  [{issue.severity}] {issue.code}: {issue.message}")
    return 0 if v.ok else 2


def cmd_chat(args) -> int:
    from .agent.orchestrator import Orchestrator

    orch = Orchestrator(PrecisionEngine(), RUNS_ROOT, model=args.model)
    print(f"Forma agent ({orch.model}). Describe the part you need. /quit to exit.\n")
    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if not user:
            continue
        if user in ("/quit", "/exit"):
            return 0
        try:
            reply = orch.send(user)
        except Exception as exc:  # bad model id, auth, rate limit — keep the REPL alive
            print(f"\n[error] {type(exc).__name__}: {exc}\n", file=sys.stderr)
            print("Check the model id and API key (e.g. --model gemini/gemini-2.5-pro).\n",
                  file=sys.stderr)
            # drop the failed user turn so history stays consistent
            if orch.messages and orch.messages[-1].get("role") == "user":
                orch.messages.pop()
            continue
        print(f"\nforma> {reply}\n")
        if orch.last_run_dir:
            print(f"(latest artifacts in {orch.last_run_dir})\n")


def main() -> int:
    parser = argparse.ArgumentParser(prog="forma")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="execute a program and validate it")
    p_run.add_argument("program", help="path to a program .py file")
    p_run.add_argument("--set", action="append", metavar="NAME=VALUE",
                       help="override a parameter (repeatable)")
    p_run.add_argument("-o", "--out", help="output directory")
    p_run.set_defaults(func=cmd_run)

    p_chat = sub.add_parser("chat", help="interactive agent session")
    p_chat.add_argument(
        "--model",
        help="LLM to drive the agent, LiteLLM format (default: $FORMA_MODEL or "
        "anthropic/claude-opus-4-8; e.g. openai/gpt-5.2, gemini/gemini-3-pro, xai/grok-4)",
    )
    p_chat.set_defaults(func=cmd_chat)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
