"""Push a scripted burst of chat at a running narrator.

    python -m tools.audience_demo                 # a realistic minute
    python -m tools.audience_demo --spam          # what a bad night looks like
    python -m tools.audience_demo --file chat.jsonl   # write, do not POST

So the audience behaviour can be watched without a live TikTok room: the
filtering, the per-user cooldown, the gift thank-you and the hosts noticing a
question are all things you otherwise only find out about in front of people.

The spam script is the more useful one. It contains the things that must NOT
reach a microphone -- a link, an @handle, a phone number, a wall of one
character -- so you can watch them be dropped rather than trust that they are.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# A minute that could actually happen. Two people, a question, a gift, and
# somebody arriving.
NORMAL: list[tuple[float, dict]] = [
    (0.0, {"kind": "join", "user": "Tomas"}),
    (1.5, {"kind": "comment", "user": "Amina", "text": "hey both"}),
    (
        4.0,
        {
            "kind": "comment",
            "user": "Amina",
            "text": "why does gold react to the dollar?",
        },
    ),
    (7.0, {"kind": "follow", "user": "Dee"}),
    (9.0, {"kind": "gift", "user": "Kwame", "gift": "a rose", "coins": 30}),
    (
        13.0,
        {
            "kind": "comment",
            "user": "Tomas",
            "text": "Mo is this the same level as yesterday",
        },
    ),
    (18.0, {"kind": "comment", "user": "Amina", "text": "makes sense, thanks"}),
    (24.0, {"kind": "gift", "user": "Sara", "gift": "a lion", "coins": 500}),
    (30.0, {"kind": "comment", "user": "Ben", "text": "what timeframe is that chart"}),
]

# Every one of these should be refused, and the tool says which were not.
SPAM: list[tuple[float, dict]] = [
    (0.0, {"kind": "comment", "user": "bot1", "text": "join t.me/freesignals now"}),
    (0.3, {"kind": "comment", "user": "bot2", "text": "dm me @goldking99"}),
    (0.6, {"kind": "comment", "user": "bot3", "text": "call +254 712 345 678"}),
    (0.9, {"kind": "comment", "user": "bot4", "text": "aaaaaaaaaaaaaaaaaaaaaaaaaaa"}),
    (1.2, {"kind": "comment", "user": "bot5", "text": "check bestsignals.com"}),
    (1.5, {"kind": "comment", "user": "loud", "text": "first"}),
    (1.7, {"kind": "comment", "user": "loud", "text": "second"}),
    (1.9, {"kind": "comment", "user": "loud", "text": "third"}),
    (2.1, {"kind": "comment", "user": "loud", "text": "fourth"}),
    (2.4, {"kind": "comment", "user": "Real", "text": "is the spread always this wide?"}),
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m tools.audience_demo", description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--spam", action="store_true", help="the burst that must be refused")
    p.add_argument("--file", help="append to a JSONL file instead of POSTing")
    p.add_argument(
        "--fast", action="store_true", help="ignore the timings and send at once"
    )
    p.add_argument("--dry-run", action="store_true", help="print, send nothing")
    return p.parse_args(argv)


def send_http(url: str, payload: dict) -> str:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return f"HTTP {response.status}"
    except urllib.error.URLError as exc:
        return f"FAILED ({exc.reason})"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    script = SPAM if args.spam else NORMAL
    url = f"http://{args.host}:{args.port}/audience"

    if args.file:
        target = f"appending to {args.file}"
    elif args.dry_run:
        target = "printing only"
    else:
        target = f"POSTing to {url}"
    print(f"{len(script)} events, {target}\n")

    started = time.monotonic()
    with contextlib.ExitStack() as stack:
        handle = (
            stack.enter_context(Path(args.file).open("a", encoding="utf-8"))
            if args.file
            else None
        )
        try:
            for at, payload in script:
                if not args.fast:
                    wait = at - (time.monotonic() - started)
                    if wait > 0:
                        time.sleep(wait)

                described = payload.get("text") or payload.get("gift") or payload["kind"]
                line = (
                    f"  {payload['kind']:<7} "
                    f"{payload.get('user', ''):<8} {described[:46]}"
                )

                if args.dry_run:
                    print(line)
                    continue
                if handle is not None:
                    handle.write(json.dumps(payload) + "\n")
                    handle.flush()
                    print(f"{line}   written")
                else:
                    print(f"{line}   {send_http(url, payload)}")
        except KeyboardInterrupt:
            return 130

    print()
    if args.spam:
        print("Every line above except Real's question should have been dropped.")
        print("Check the narrator's status bar: `audience` counts what it refused,")
        print("and none of those links should ever be spoken.")
    else:
        print("Watch for: a thank-you to Kwame and Sara within a few seconds, and")
        print("Amina's question answered in the hosts' own words rather than read out.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
