"""Does Unreal answer, and does the presenter actor have the five functions.

    python -m tools.unreal_check                # ping, describe, play a nod
    python -m tools.unreal_check --no-gesture   # check only; move nothing
    python -m tools.unreal_check --gesture shrug
    python -m tools.unreal_check --json         # the raw describe reply

Run this after `python -m tools.livelink_check --blink` has proved the face,
not before. The two halves of the character fail in completely different ways
and diagnosing them together is how an afternoon goes: Live Link is UDP and
silent, this is HTTP and talkative, and there is no failure mode they share.

Exits non-zero when anything below is wrong, so it can go in a pre-stream
script and be believed.

WHAT EACH LINE MEANS WHEN IT FAILS
  ping        the Remote Control *web server* is not running. Enabling the
              plugin is not the same thing as starting the server: tick
              "Start Web Control Server on Startup" in Project Settings, or
              run `WebControl.StartServer` in the editor console.
  describe    the server answered but the object path does not resolve. The
              path is per-level and changes when the actor is renamed or
              duplicated, so a path that worked last week is not evidence.
  functions   the actor resolved but a function is missing, or its input pins
              are named differently. Remote Control matches pin names, and a
              pin called `Name` where the contract says `name` fails with a
              400 whose body says only that a parameter could not be found.

For `transport = "osc"` there is nothing to ping and nothing to describe --
UDP does not answer -- so this sends the gesture and says plainly that it
cannot tell you whether anything received it. That is not a shortcoming of
the tool; it is the reason `remote_control` is the better of the two.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from urllib import error as urlerror

from narrator.avatar.unreal import (
    FUNCTIONS,
    NullTransport,
    OscTransport,
    RemoteControlTransport,
    build_transport,
)
from narrator.config import load_config

TICK = "OK  "
CROSS = "MISS"


def describe_functions(payload: dict[str, Any]) -> dict[str, list[str]]:
    """Function name -> input pin names, out of a /remote/object/describe reply.

    Unreal has moved this shape between versions -- arguments have appeared
    under `Arguments`, `Args` and `Parameters`, and the name key has been both
    `Name` and `Description`. All of them are read, because the alternative is
    a tool that reports MISS on a correctly built Blueprint the week after an
    engine upgrade, which teaches an operator to stop believing it.
    """
    found: dict[str, list[str]] = {}
    for entry in payload.get("Functions") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("Name") or entry.get("FunctionName") or ""
        if not name:
            continue
        arguments = (
            entry.get("Arguments") or entry.get("Args") or entry.get("Parameters") or []
        )
        pins: list[str] = []
        for argument in arguments:
            if isinstance(argument, dict):
                pins.append(argument.get("Name") or argument.get("Description") or "")
            else:
                pins.append(str(argument))
        found[name] = pins
    return found


def resolved_path(described: dict[str, Any], fallback: str) -> str:
    """What the editor called the actor, or what we asked for if it did not say."""
    for key in ("Path", "ObjectPath", "PathName"):
        value = described.get(key)
        if isinstance(value, str) and value:
            return value
    return fallback


def print_path(path: str) -> None:
    """The one line an operator is here for: pasteable into config.toml.

    Printed verbatim and on its own line, because config.toml's comment for
    this field promises that this tool prints it and an operator should be
    able to copy the whole line without editing it.
    """
    print("\n  paste this into config.toml under [character.unreal]:\n")
    print(f'    object_path = "{path}"')


def check_osc(transport: OscTransport, *, gesture: str | None) -> bool:
    print(f"\ntransport      osc -> {transport.host}:{transport.port}")
    print("\n  OSC is UDP and does not answer. Nothing here can be verified;")
    print("  put a Print String on the OSC Server node and watch the viewport.")
    if gesture:
        try:
            transport.call_now("PlayGesture", name=gesture.replace("-", "_"))
        except OSError as exc:
            print(f"\n  [{CROSS}] could not send: {exc}")
            return False
        print(f"\n  [{TICK}] sent PlayGesture({gesture!r}) -- delivery unknown")
    return True


def check_remote(transport: RemoteControlTransport, *, gesture: str | None) -> bool:
    print(f"\ntransport      remote_control -> {transport.url}")

    try:
        info = transport.info()
    except (urlerror.URLError, OSError, ValueError) as exc:
        print(f"\n  [{CROSS}] ping           {exc}")
        print(
            "\n  Remote Control's web server is not answering. Enabling the\n"
            "  plugin does not start it: tick 'Start Web Control Server on\n"
            "  Startup' in Project Settings -> Plugins -> Remote Control API,\n"
            "  or run WebControl.StartServer in the editor console."
        )
        return False
    version = info.get("EngineVersion") or info.get("Version") or "?"
    print(f"\n  [{TICK}] ping           Unreal {version}")

    if not transport.object_path:
        print(f"  [{CROSS}] object path    empty")
        print(
            "\n  Right-click the presenter actor in the World Outliner ->\n"
            "  Copy Reference. It is per-level and changes if the actor is\n"
            "  renamed or duplicated."
        )
        return False

    try:
        described = transport.describe()
    except (urlerror.URLError, OSError, ValueError) as exc:
        print(f"  [{CROSS}] describe       {exc}")
        print(f"\n  The path did not resolve:\n    {transport.object_path}")
        return False
    print(f"  [{TICK}] describe       {described.get('Class', '?')}")

    print("\n  the contract (UNREAL_SETUP.md)")
    ok = True
    found = describe_functions(described)
    for name, expected in FUNCTIONS.items():
        pins = found.get(name)
        if pins is None:
            print(f"    [{CROSS}] {name}({', '.join(expected)}) -- not on this actor")
            ok = False
            continue
        missing = [pin for pin in expected if pin not in pins]
        if missing:
            print(
                f"    [{CROSS}] {name} has pins {pins}, "
                f"expected {list(expected)} -- missing {missing}"
            )
            ok = False
        else:
            print(f"    [{TICK}] {name}({', '.join(expected)})")

    if gesture and ok:
        try:
            transport.call_now("PlayGesture", name=gesture.replace("-", "_"))
        except Exception as exc:
            print(f"\n  [{CROSS}] PlayGesture({gesture!r}) {exc}")
            ok = False
        else:
            print(f"\n  [{TICK}] played {gesture!r} -- watch the character")
    elif gesture:
        print(f"\n  skipped the {gesture!r} gesture; fix the contract first")

    print_path(resolved_path(described, transport.object_path))
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=None, help="path to config.toml")
    parser.add_argument("--gesture", default="nod", help="gesture to play (default nod)")
    parser.add_argument(
        "--no-gesture", action="store_true", help="check only; move nothing"
    )
    parser.add_argument("--json", action="store_true", help="dump the describe reply")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if not cfg.character.enabled:
        # Checked but not enforced: an operator wiring Unreal up for the first
        # time has every reason to run this before turning the character on.
        print("\nnote: [character] enabled = false -- checking anyway")
        cfg.character.enabled = True

    transport = build_transport(cfg)
    gesture = None if args.no_gesture else args.gesture

    if isinstance(transport, NullTransport):
        print(
            "\n[character.unreal] transport is 'none', or object_path is empty.\n"
            "Nothing to check. The face does not need any of this -- run\n"
            "`python -m tools.livelink_check --blink` for that half."
        )
        return 1

    if args.json:
        if not isinstance(transport, RemoteControlTransport):
            print("--json needs transport = 'remote_control'")
            return 1
        try:
            print(json.dumps(transport.describe(), indent=2)[:8000])
        except Exception as exc:
            print(f"describe failed: {exc}")
            return 1
        return 0

    if isinstance(transport, OscTransport):
        ok = check_osc(transport, gesture=gesture)
    else:
        assert isinstance(transport, RemoteControlTransport)
        ok = check_remote(transport, gesture=gesture)

    print()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
