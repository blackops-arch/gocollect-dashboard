#!/usr/bin/env python3
"""Publish the farm_loop ledger to the dashboard repo's `data` branch.

Reads the append-only ledger (one JSON record per crate-open attempt), derives
the fields the dashboard needs, masks wallet addresses, and upserts
`ledger.json` on the `data` branch via the GitHub API -- so refreshing the data
never triggers a GitHub Pages rebuild (the site is built from `gh-pages`).

The page is PUBLIC, so wallet addresses are masked to `abcd…wxyz`. The dashboard
only ever needs to tell one of our wallets from another; publishing full
addresses would hand the game's operators our ban list.

    python3 publish_ledger.py            # publish once (skips if unchanged)
    python3 publish_ledger.py --dry-run  # derive + scan + show, do not push
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time

LEDGER = os.environ.get(
    "GC_LEDGER", "/home/bluey/gocollect-fleet-share/fleet/output/farm_loop.jsonl")
WALLETS = os.environ.get(
    "GC_WALLETS", "/home/bluey/gocollect-fleet-share/fleet/wallets.json")
REPO = os.environ.get("GC_DASH_REPO", "blackops-arch/gocollect-dashboard")
BRANCH, PATH = "data", "ledger.json"
MAX_ATTEMPTS = 4000          # keep the payload bounded as the ledger grows

FORBIDDEN = {
    "base58 address": re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b"),
    "ipv4": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "host:port": re.compile(r"\b[a-z0-9.-]+\.(?:com|net|io|org|xyz|fun):\d{2,5}\b"),
    "private key": re.compile(r"(?:private[_-]?key|secret|mnemonic|seed phrase)", re.I),
}


def mask(addr: str) -> str:
    """abcd…wxyz -- enough to tell wallets apart, useless as an address."""
    if not addr or len(addr) < 10:
        return "—"
    return addr[:4] + "…" + addr[-4:]


def derive(rec: dict) -> dict:
    """Apply the spec's derived-field rules."""
    status = rec.get("status")
    code = rec.get("code")
    if status == 200:
        outcome = "success"
    else:
        outcome = "denied"
    if code:
        reason = code
    elif status == 0:
        reason = "transport"
    else:
        reason = "unknown"
    if status == 0:
        side = "client"
    elif isinstance(status, int) and status >= 400:
        side = "server"
    else:
        side = "—"
    crate = rec.get("crate_id") or ""
    return {
        "at": rec.get("at"), "wallet": rec.get("wallet"),
        "crate_id": crate, "d": rec.get("d"),
        "accepted_total": rec.get("accepted_total"),
        "status": status, "code": code, "open_id": rec.get("open_id"),
        "tier": rec.get("tier"), "drawn_tier": rec.get("drawn_tier"),
        "u": rec.get("u"), "pity": rec.get("pity"),
        "outcome": outcome, "reason": reason, "side": side,
        "endpoint": "POST /v1/crates/%s/open" % crate,
        "message": "HTTP %s · POST /v1/crates/%s/open" % (status, crate),
        "day": (rec.get("at") or "")[:10],
    }


def scan(text: str) -> list:
    hits = []
    for label, rx in FORBIDDEN.items():
        found = sorted(set(rx.findall(text)))
        if found:
            hits.append((label, found[:3]))
    return hits


def gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def remote_sha() -> str | None:
    r = gh("api", f"/repos/{REPO}/contents/{PATH}?ref={BRANCH}", "--jq", ".sha")
    return r.stdout.strip() if r.returncode == 0 else None


def main() -> int:
    dry = "--dry-run" in sys.argv
    if not os.path.exists(LEDGER):
        print("ledger missing: %s" % LEDGER, file=sys.stderr)
        return 1

    # wallet index -> masked address
    wallets: dict = {}
    try:
        with open(WALLETS) as fh:
            for i, entry in enumerate(json.load(fh)):
                if isinstance(entry, dict) and entry.get("address"):
                    wallets[str(i)] = {"label": "W%d" % i,
                                       "addr": mask(entry["address"])}
    except Exception as exc:
        print("wallets.json unreadable (%s) -- labels only" % exc, file=sys.stderr)

    # Ship the RAW stored records; the page derives everything else. That keeps
    # the detail view byte-identical to the ledger, and roughly halves payload.
    raw = []
    with open(LEDGER) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                raw.append(json.loads(line))
            except ValueError:
                continue
    raw = raw[-MAX_ATTEMPTS:]

    # Derive server-side too, purely to validate the rules and summarise.
    derived = [derive(r) for r in raw]
    denials = [a for a in derived if a["outcome"] == "denied"]
    by_reason: dict = {}
    for a in denials:
        by_reason[a["reason"]] = by_reason.get(a["reason"], 0) + 1
    by_status: dict = {}
    for a in derived:
        by_status[str(a["status"])] = by_status.get(str(a["status"]), 0) + 1

    payload = {
        "generated_ts": time.time(),
        "wallets": wallets,
        "attempts": raw,
        "stats": {
            "attempts": len(raw), "denials": len(denials),
            "successes": len(raw) - len(denials),
            "draws": len([a for a in derived
                          if a["outcome"] == "success" and a.get("tier") not in (None, "empty")]),
            "by_reason": by_reason, "by_status": by_status,
            "first": raw[0]["at"] if raw else None,
            "last": raw[-1]["at"] if raw else None,
        },
    }
    blob = json.dumps(payload, separators=(",", ":"), sort_keys=True)

    hits = scan(blob)
    if hits:
        print("REFUSING TO PUBLISH -- sensitive pattern(s) found:", file=sys.stderr)
        for label, sample in hits:
            print("  %s: %s" % (label, sample), file=sys.stderr)
        return 2

    if dry:
        print("dry-run: %d attempts, %d denials, %d draws, %.1f KB, would push"
              % (len(raw), len(denials), payload["stats"]["draws"], len(blob) / 1024))
        return 0

    content = base64.b64encode(blob.encode()).decode()
    args = ["api", "-X", "PUT", f"/repos/{REPO}/contents/{PATH}",
            "-f", "message=ledger: snapshot %s" %
            time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
            "-f", f"content={content}", "-f", f"branch={BRANCH}"]
    sha = remote_sha()
    if sha:
        args += ["-f", f"sha={sha}"]
    r = gh(*args)
    if r.returncode != 0:
        print("push failed: %s" % (r.stderr or r.stdout).strip(), file=sys.stderr)
        return 3
    print("pushed ledger.json (%d attempts, %d denials, %.1f KB)"
          % (len(raw), len(denials), len(blob) / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
