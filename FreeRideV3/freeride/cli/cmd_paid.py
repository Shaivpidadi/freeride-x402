"""``freeride paid`` — the demo switch for the Hedera cash lane.

Free-first means the paid lane never shows itself while free works. That is
the product being correct and also the reason it is hard to demonstrate: the
only other way to see it is to break your own provider keys.

This flips the running daemon, so a demo can go free -> paid -> free without
restarting anything. It is process-local on purpose: a restart returns to
whatever ``~/.freeride/.env`` says, so an interrupted demo cannot leave the
wallet paying for every request forever.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:11343"
TIMEOUT_S = 10


def _request(path: str, payload: dict | None = None) -> tuple[int, dict]:
    url = f"{DEFAULT_URL}{path}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 - fixed localhost URL
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}
    except Exception:
        return 0, {}


def _show(force_paid: bool) -> None:
    if force_paid:
        print("  paid lane : ON  - every request settles on Hedera; free is skipped")
        print("  turn off  : freeride paid off")
    else:
        print("  paid lane : off - free providers first, pay only when free cannot serve")
        print("  turn on   : freeride paid on")


def cmd_paid(args) -> int:
    state = getattr(args, "state", None)

    if state is None:
        status, body = _request("/v1/_freeride/x402/policy")
        if status != 200:
            print("  daemon not reachable on 127.0.0.1:11343 — run `freeride start`.")
            return 1
        _show(bool(body.get("force_paid")))
        return 0

    turn_on = state == "on"
    status, body = _request("/v1/_freeride/x402/force-paid", {"on": turn_on})
    if status == 0:
        print("  daemon not reachable on 127.0.0.1:11343 — run `freeride start`.")
        return 1
    if status != 200:
        error = (body.get("error") or {}).get("message") or f"HTTP {status}"
        print(f"  could not change it: {error}")
        return 1

    _show(turn_on)
    if turn_on:
        print()
        print("  This spends real HBAR on every request. It resets on daemon restart.")
    return 0
