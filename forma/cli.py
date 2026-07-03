"""Forma CLI.

  uv run forma            # start the web app (the one command you need)
  uv run forma run examples/simple_box.py --set length=200   # run a program
  uv run forma chat       # terminal agent session
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import config
from .engines.precision import PrecisionEngine

# standalone CLI runs (not project-scoped) land here
CLI_RUNS = config.DATA_DIR / "cli-runs"


def cmd_serve(args) -> int:
    """Start the web app. Auto-reload is on and safe (all runtime data lives in
    ~/.forma, outside the repo, so builds never trigger the reloader). Watches
    only the forma/ package."""
    import uvicorn

    print(f"\n  forma → http://{args.host}:{args.port}\n")
    uvicorn.run(
        "forma.api.app:app",
        host=args.host,
        port=args.port,
        reload=not args.no_reload,
        reload_dirs=[str(Path(__file__).parent)],
    )
    return 0


def cmd_run(args) -> int:
    code = Path(args.program).read_text()
    params = {}
    for pair in args.set or []:
        name, _, value = pair.partition("=")
        params[name] = value
    run_dir = Path(args.out) if args.out else CLI_RUNS / time.strftime("%Y%m%d-%H%M%S")

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

    orch = Orchestrator(PrecisionEngine(), CLI_RUNS, model=args.model)
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
        reply = orch.send(user)
        print(f"\nforma> {reply}\n")
        if orch.last_run_dir:
            print(f"(latest artifacts in {orch.last_run_dir})\n")


def main() -> int:
    parser = argparse.ArgumentParser(prog="forma")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="run the web app (default)")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--no-reload", action="store_true", help="disable auto-reload")
    p_serve.set_defaults(func=cmd_serve)

    p_run = sub.add_parser("run", help="execute a program and validate it")
    p_run.add_argument("program", help="path to a program .py file")
    p_run.add_argument("--set", action="append", metavar="NAME=VALUE",
                       help="override a parameter (repeatable)")
    p_run.add_argument("-o", "--out", help="output directory")
    p_run.set_defaults(func=cmd_run)

    p_chat = sub.add_parser("chat", help="terminal agent session")
    p_chat.add_argument("--model", help="LLM in LiteLLM format (default: $FORMA_MODEL "
                        "or anthropic/claude-opus-4-8)")
    p_chat.set_defaults(func=cmd_chat)

    # `forma` with no subcommand starts the web app (the default)
    argv = sys.argv[1:]
    known = {"serve", "run", "chat", "-h", "--help"}
    if not argv or argv[0] not in known:
        argv = ["serve", *argv]
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
