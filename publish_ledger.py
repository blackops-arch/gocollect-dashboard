#!/usr/bin/env python3
"""Publish the farm_loop ledger + card pulls to the dashboard repo's `data` branch.

Reads the append-only ledger (one JSON record per crate-open attempt), attaches
a masked wallet-address map, pulls each wallet's public card list, and upserts
`ledger.json` on the `data` branch via the GitHub API -- so refreshing the data
never triggers a GitHub Pages rebuild (the site is built from `gh-pages`).

The page is PUBLIC, so wallet addresses are masked to `abcd…wxyz`. The dashboard
only needs to tell one of our wallets from another; publishing full addresses
would hand the game's operators our ban list. Card mints are public asset IDs and
are kept in full -- the leak guard whitelists them explicitly.

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
import tempfile
import time
import urllib.request

LEDGER = os.environ.get(
    "GC_LEDGER", "/home/bluey/gocollect-fleet-share/fleet/output/farm_loop.jsonl")
WALLETS = os.environ.get(
    "GC_WALLETS", "/home/bluey/gocollect-fleet-share/fleet/wallets.json")
SLOT_LOG = os.environ.get(
    "GC_SLOT_LOG", "/home/bluey/gocollect-fleet-share/fleet/output/farm_multi_%d.log")
REPO = os.environ.get("GC_DASH_REPO", "blackops-arch/gocollect-dashboard")
BRANCH, PATH = "data", "ledger.json"
MAX_ATTEMPTS = 4000          # keep the payload bounded as the ledger grows
PLAYER = "https://gocollect.fun/v1/players/%s"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

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
    """Apply the spec's derived-field rules (used for validation + summary)."""
    status = rec.get("status")
    code = rec.get("code")
    outcome = "success" if status == 200 else "denied"
    reason = code or ("transport" if status == 0 else "unknown")
    side = "client" if status == 0 else ("server" if isinstance(status, int) and status >= 400 else "—")
    crate = rec.get("crate_id") or ""
    return {
        "outcome": outcome, "reason": reason, "side": side,
        "endpoint": "POST /v1/crates/%s/open" % crate,
        "message": "HTTP %s · POST /v1/crates/%s/open" % (status, crate),
        "tier": rec.get("tier"),
    }


def pulls_for(addr: str) -> tuple[list, dict]:
    """Awarded cards + stats for one wallet. Public GET -- no auth, no Turnstile."""
    req = urllib.request.Request(PLAYER % addr,
                                 headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.loads(r.read())
    return (data.get("pulls") or []), (data.get("stats") or {})


def review_from_logs(slots: list) -> list:
    """Cards the server denied or is still holding, read from the slots' own logs.

    Only the `review={...}` JSON is taken -- never the surrounding log line, which
    can carry proxy endpoints.
    """
    out = []
    for s in slots:
        path = SLOT_LOG % s
        if not os.path.exists(path):
            continue
        last = None
        try:
            with open(path, errors="replace") as fh:
                for line in fh:
                    if "review=" in line:
                        m = re.search(r"review=(\{.*\})\s*$", line.strip())
                        if m:
                            last = m.group(1)
        except OSError:
            continue
        if not last:
            continue
        try:
            rv = json.loads(last)
        except ValueError:
            continue
        for d in (rv.get("denied") or []):
            out.append({"wallet": s, "status": "denied", "raw": d})
        for w in (rv.get("waiting") or []):
            out.append({"wallet": s, "status": "pending", "raw": w})
    return out


def scan(text: str, allow: set) -> list:
    """Leak guard. `allow` holds public identifiers (card mints) to ignore."""
    scrubbed = text
    for tok in allow:
        scrubbed = scrubbed.replace(tok, "")
    hits = []
    for label, rx in FORBIDDEN.items():
        found = sorted(set(rx.findall(scrubbed)))
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

    entries: list = []
    try:
        with open(WALLETS) as fh:
            entries = json.load(fh)
    except Exception as exc:
        print("wallets.json unreadable (%s) -- labels only" % exc, file=sys.stderr)

    wallets: dict = {}
    addr_by_idx: dict = {}
    for i, entry in enumerate(entries):
        if isinstance(entry, dict) and entry.get("address"):
            wallets[str(i)] = {"label": "G%d" % i, "addr": mask(entry["address"])}
            addr_by_idx[i] = entry["address"]

    # Ship the RAW stored records; the page derives everything else. That keeps
    # the detail view byte-identical to the ledger and halves the payload.
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

    # --- cards: awarded pulls (public GET per wallet) ---
    cards: list = []
    pull_stats: dict = {}
    for idx in sorted(addr_by_idx):
        try:
            pulls, stats = pulls_for(addr_by_idx[idx])
        except Exception as exc:
            print("  G%d pulls unavailable: %s" % (idx, str(exc)[:70]), file=sys.stderr)
            continue
        pull_stats[str(idx)] = stats
        for p in pulls:
            cards.append({
                "wallet": idx,
                "status": "cashed-out" if p.get("cashedOut") else "approved",
                "tier": p.get("tier"), "name": p.get("name"), "grade": p.get("grade"),
                "valueUsd": p.get("valueUsd"), "day": p.get("day"),
                "mint": p.get("mint"), "image": p.get("image"),
                "times": p.get("times"), "soldUsd": p.get("soldUsd"),
            })

    # --- cards: denied / still-pending, from the slots' review blocks ---
    cards += review_from_logs(sorted(addr_by_idx))

    derived = [derive(r) for r in raw]
    denials = [a for a in derived if a["outcome"] == "denied"]
    by_reason: dict = {}
    for a in denials:
        by_reason[a["reason"]] = by_reason.get(a["reason"], 0) + 1

    by_tier: dict = {}
    for c in cards:
        if c.get("tier"):
            by_tier[c["tier"]] = by_tier.get(c["tier"], 0) + 1
    by_status: dict = {}
    for c in cards:
        by_status[c["status"]] = by_status.get(c["status"], 0) + 1

    payload = {
        "generated_ts": time.time(),
        "wallets": wallets,
        "attempts": raw,
        "cards": cards,
        "pull_stats": pull_stats,
        "stats": {
            "attempts": len(raw), "denials": len(denials),
            "successes": len(raw) - len(denials),
            "draws": len([a for a in derived
                          if a["outcome"] == "success" and a.get("tier") not in (None, "empty")]),
            "by_reason": by_reason,
            "cards": len(cards), "cards_by_tier": by_tier, "cards_by_status": by_status,
            "cards_value_usd": sum(c.get("valueUsd") or 0 for c in cards),
            "first": raw[0]["at"] if raw else None,
            "last": raw[-1]["at"] if raw else None,
        },
    }
    blob = json.dumps(payload, separators=(",", ":"), sort_keys=True)

    allow = {c["mint"] for c in cards if c.get("mint")}
    hits = scan(blob, allow)
    if hits:
        print("REFUSING TO PUBLISH -- sensitive pattern(s) found:", file=sys.stderr)
        for label, sample in hits:
            print("  %s: %s" % (label, sample), file=sys.stderr)
        return 2

    if dry:
        print("dry-run: %d attempts, %d denials, %d cards (%.1f KB), would push"
              % (len(raw), len(denials), len(cards), len(blob) / 1024))
        for c in cards[:6]:
            print("  G%s %-10s %-9s %s" % (c["wallet"], c["status"], c.get("tier"),
                                           (c.get("name") or "")[:52]))
        return 0

    # --input (a file) avoids the ARG_MAX limit that a base64 argv would hit.
    body = {
        "message": "ledger: snapshot %s" % time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "content": base64.b64encode(blob.encode()).decode(),
        "branch": BRANCH,
    }
    sha = remote_sha()
    if sha:
        body["sha"] = sha
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(body, fh)
        tmp = fh.name
    try:
        r = gh("api", "-X", "PUT", f"/repos/{REPO}/contents/{PATH}", "--input", tmp)
    finally:
        os.unlink(tmp)
    if r.returncode != 0:
        print("push failed: %s" % (r.stderr or r.stdout).strip(), file=sys.stderr)
        return 3
    print("pushed ledger.json (%d attempts, %d denials, %d cards, %.1f KB)"
          % (len(raw), len(denials), len(cards), len(blob) / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
