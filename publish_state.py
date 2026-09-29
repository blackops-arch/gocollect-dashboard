#!/usr/bin/env python3
"""Publish a sanitized fleet snapshot to the dashboard repo's `data` branch.

Reads the local read-only dashboard (127.0.0.1:8790/api/state) and upserts
`state.json` on the `data` branch via the GitHub API, so refreshing the data
never triggers a GitHub Pages rebuild (the page is built from `gh-pages`).

Safety gate: refuses to publish if the payload contains anything that looks
like a wallet address, an IP, or a proxy host:port. The source endpoint is
already sanitized; this is defence in depth so a future edit can't leak.

    python3 publish_state.py            # publish once (skips if unchanged)
    python3 publish_state.py --dry-run  # scan + show, do not push
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

SRC = os.environ.get("GC_STATE_URL", "http://127.0.0.1:8790/api/state")
REPO = os.environ.get("GC_DASH_REPO", "blackops-arch/gocollect-dashboard")
BRANCH = "data"
PATH = "state.json"

FORBIDDEN = {
    "base58 address": re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b"),
    "ipv4": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "host:port": re.compile(r"\b[a-z0-9.-]+\.(?:com|net|io|org|xyz|fun):\d{2,5}\b"),
    "private key": re.compile(r"(?:private[_-]?key|secret|mnemonic|seed phrase)", re.I),
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


def remote_state() -> str | None:
    r = gh("api", f"/repos/{REPO}/contents/{PATH}?ref={BRANCH}", "--jq", ".content")
    if r.returncode != 0:
        return None
    try:
        return base64.b64decode(r.stdout.strip() + "===").decode()
    except Exception:
        return None


def remote_sha() -> str | None:
    r = gh("api", f"/repos/{REPO}/contents/{PATH}?ref={BRANCH}", "--jq", ".sha")
    return r.stdout.strip() if r.returncode == 0 else None


def main() -> int:
    dry = "--dry-run" in sys.argv
    try:
        with urllib.request.urlopen(SRC, timeout=15) as r:
            state = json.load(r)
    except Exception as exc:
        print("source unreachable (%s) -- nothing published" % exc, file=sys.stderr)
        return 1

    state["generated_ts"] = time.time()
    blob = json.dumps(state, indent=1, sort_keys=True)

    hits = scan(blob)
    if hits:
        print("REFUSING TO PUBLISH -- sensitive pattern(s) found:", file=sys.stderr)
        for label, sample in hits:
            print("  %s: %s" % (label, sample), file=sys.stderr)
        return 2

    prev = remote_state()
    if prev == blob:
        print("unchanged -- no push")
        return 0
    if dry:
        print("dry-run: %d bytes, %d wallets, would push" %
              (len(blob), len(state.get("wallets", []))))
        return 0

    content = base64.b64encode(blob.encode()).decode()
    args = ["api", "-X", "PUT", f"/repos/{REPO}/contents/{PATH}",
            "-f", "message=state: fleet snapshot %s" %
            time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
            "-f", f"content={content}", "-f", f"branch={BRANCH}"]
    sha = remote_sha()
    if sha:
        args += ["-f", f"sha={sha}"]
    r = gh(*args)
    if r.returncode != 0:
        print("push failed: %s" % (r.stderr or r.stdout).strip(), file=sys.stderr)
        return 3
    print("pushed state.json (%d bytes, %d wallets)" %
          (len(blob), len(state.get("wallets", []))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
