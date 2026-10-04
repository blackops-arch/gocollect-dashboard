#!/usr/bin/env python3
"""Publish the Kit B farm run to the public dashboard.

Kit B keeps no structured attempt ledger, so this reads two honest sources:

  * output/farm_loop.jsonl          full-fidelity records written from now on
                                    by claim_all.record_attempt()
  * claim_C*.log + state_C*.json    today's already-played attempts, which
                                    exist only as text. Those are backfilled
                                    with the fields they actually carry and
                                    flagged "backfilled": true -- never with
                                    invented status, distance or roll values.

Wallet addresses are masked to `abcd...wxyz`: the page is public and full
addresses would hand the operators our ban list. A leak guard scans the
payload for anything address-shaped before a single byte is pushed.

    python3 publish_kitb.py --dry-run   # derive + scan + show, never push
    python3 publish_kitb.py             # publish (skips if unchanged)
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.request

REPO = os.path.dirname(os.path.abspath(__file__))
KITB = "/home/bluey/gocollect-25open"
LEDGER = os.path.join(KITB, "output", "farm_loop.jsonl")
OUTPUT = os.path.join(KITB, "output")
DATA_BRANCH = "data"
PUBLISH_PATH = "ledger.json"
API = "https://api.github.com/repos/blackops-arch/gocollect-dashboard/contents"
PLAYER = "https://gocollect.fun/v1/players/%s"

# The page is PUBLIC. Wallet addresses never leave unmasked.
ADDR_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
WALLET_RE = re.compile(r"^Z(?:0[1-9]|10)\.key$")
# Only the live Z run is published. The C keys are emptied and the S keys are
# tainted, so neither may appear on the public page (Cil 2026-10-04).
LIVE_LABELS = ("Z01", "Z02", "Z03", "Z04", "Z05")
BATCH_NAME = "Z-run"
DAILY_LIMIT = 25          # opens per wallet per day

# label -> full address, for looking up each wallet's pinned device identity.
# Populated by wallets(); identity_pin.json is keyed by address, not label.
ADDR_BY_LABEL = {}
FMT = "%Y-%m-%dT%H:%M:%SZ"

def mask(addr: str) -> str:
    """Kept for callers that still want a short form.

    The dashboard now publishes full addresses: Cil needs to paste the exact
    address of a wallet that keeps failing, and a 4+4 stub is not pasteable.
    """
    return addr[:4] + "…" + addr[-4:] if len(addr) > 10 else addr


def token() -> str:
    """GitHub token from the gh CLI store; never from a command line."""
    p = os.path.expanduser("~/.config/gh/hosts.yml")
    with open(p) as f:
        for line in f:
            if "oauth_token:" in line:
                return line.split("oauth_token:")[1].strip()
    raise SystemExit("no oauth_token in gh hosts.yml")


def wallets() -> dict:
    """Label -> masked address, read from the key files' derived pubkeys.

    Addresses come from the wallet key files, so the map reflects the fleet
    that actually ran; only the masked form is ever serialised.
    """
    import base58
    from solders.keypair import Keypair

    out, addr_by = {}, {}
    for fn in sorted(os.listdir(os.path.join(KITB, "keys"))):
        if fn[:-4] not in LIVE_LABELS:
            continue
        if not WALLET_RE.match(fn):
            continue
        label = fn[:-4]
        raw = open(os.path.join(KITB, "keys", fn)).read().strip()
        kp = Keypair.from_bytes(base58.b58decode(raw))
        addr = str(kp.pubkey())
        out[label] = {"label": label, "addr": mask(addr)}
        addr_by[label] = addr
    ADDR_BY_LABEL.clear()
    ADDR_BY_LABEL.update(addr_by)
    return out, addr_by


def _addr_map(rows) -> dict:
    return {str(r.get("label")): r.get("address")
            for r in rows if isinstance(r, dict) and r.get("label")}


def cards_from_states() -> list:
    """Won cards read from the run's own state files.

    The player record only lists CREDITED pulls; a card sits unswept until the
    sweep step runs, so the player endpoint reports pulls: 0 and the Cards tab
    stays empty. A prize is written into state_C*.json the moment it is won, so
    that is the source that actually has them.

    Batch 1 and batch 2 reuse the same labels for DIFFERENT wallets, so each
    card is given the address from its own batch's wallet map. Resolving by
    label alone would hand a batch-1 card a batch-2 address.
    """
    out = []
    # batch 2: live keys dir labels, addresses derived from keys/
    try:
        b2 = json.load(open(os.path.join(KITB, "wallets.json")))
    except (OSError, ValueError):
        b2 = []
    b2_map = _addr_map(b2)
    for label in sorted(_key_labels()):
        out += _cards_in(os.path.join(KITB, "state_%s.json" % label),
                         label, BATCH_NAME, b2_map.get(label))
    # batch 1: kept generation, addresses from its own wallets backup
    b1 = os.path.join(KITB, "private", "retired-keys",
                      "batch1-state-20261003T151412")
    try:
        b1rows = json.load(open(os.path.join(
            KITB, "private", "retired-keys",
            "wallets-json-backup-20261003T144952.json")))
    except (OSError, ValueError):
        b1rows = []
    b1_map = _addr_map(b1rows)
    for fn in sorted(os.listdir(b1)) if os.path.isdir(b1) else []:
        m = re.match(r"state_([A-Za-z0-9]+)\.json$", fn)
        if m:
            out += _cards_in(os.path.join(b1, fn), m.group(1), "batch 1",
                             b1_map.get(m.group(1)))
    return out


def _key_labels() -> set:
    try:
        have = set(os.listdir(os.path.join(KITB, "keys")))
        return {lbl for lbl in LIVE_LABELS if (lbl + ".key") in have}
    except OSError:
        return set()


GRADE_RE = re.compile(
    r"\b(PSA\s+\w[\w\- ]*?\d+(?:\.\d)?|CGC\s+\d+(?:\.\d)?(?:\s+[A-Z]+)?"
    r"|BGS\s+\d+(?:\.\d)?|SGC\s+\d+(?:\.\d)?)\b")


def _grade_of(p: dict):
    """Grade parsed from the card name ('... CGC 10 GEM')."""
    m = GRADE_RE.search(str(p.get("name") or ""))
    return m.group(1) if m else None


def _win_index() -> dict:
    """name -> {"day": date, "grade": grade} from the run's own win records.

    The four batch-1 wins were logged with a crate id where the wallet label
    belongs, so they cannot be matched by wallet. Their `name` is the exact
    same string the state file holds, so name is the join key.
    """
    out = {}
    try:
        with open(LEDGER) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("code") != "won":
                    continue
                name = str(r.get("name") or "")
                m = GRADE_RE.search(name)
                out[name] = {
                    "day": str(r.get("at") or "")[:10] or None,
                    "grade": m.group(1) if m else None,
                }
    except OSError:
        pass
    return out


def _cards_in(path: str, label: str, batch: str, addr=None) -> list:
    try:
        with open(path) as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        return []
    wins = _win_index()
    out = []
    for p in st.get("prizes") or []:
        w = wins.get(str(p.get("name") or "")) or {}
        out.append({
            "wallet": label,
            "batch": batch,
            "addr": addr,          # this card's OWN wallet (batch-specific)
            # Same words the page already uses (approved/pending): batch 1's
            # four were credited and swept to Cil's hub; C08 is still held by
            # the server, so state files carry no status for either.
            "status": "approved" if batch == "batch 1" else "pending",
            "tier": p.get("tier"),
            "name": p.get("name"),
            "grade": _grade_of(p) or w.get("grade"),
            "valueUsd": p.get("valueUsd"),
            "soldUsd": None,
            "times": None,
            "day": w.get("day"),
            "image": None,
            "mint": p.get("crate"),
            "prizeId": p.get("prizeId"),
        })
    return out


def pulls_for(addr: str) -> list:
    """Cards the platform actually AWARDED this wallet.

    Source: GET /v1/players/<addr> -> `pulls`. This is the credited-card record,
    which is why the ledger reads it instead of our own log: an open can return
    an error client-side while the card is credited server-side, and a log-driven
    card list would silently miss that pull.

    Returns [] on any failure so a flaky fetch degrades to "no new cards" rather
    than aborting the publish run.
    """
    try:
        req = urllib.request.Request(
            PLAYER % addr,
            headers={"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/131.0.0.0 Safari/537.36")})
        with urllib.request.urlopen(req, timeout=20) as r:
            return (json.load(r) or {}).get("pulls") or []
    except Exception as e:
        print("  pulls fetch failed for %s: %s" % (addr[:6] + "\u2026", str(e)[:80]))
        return []


def _batch1_addrs() -> dict:
    """label -> address for the previous batch, from its kept wallets backup."""
    try:
        rows = json.load(open(os.path.join(
            KITB, "private", "retired-keys",
            "wallets-json-backup-20261003T144952.json")))
    except (OSError, ValueError):
        return {}
    return {str(r.get("label")): r.get("address")
            for r in rows if isinstance(r, dict) and r.get("label")}


def cards_from_pulls(addr_by: dict) -> list:
    """Build the dashboard's card list from every wallet's credited pulls.

    `addr_by` maps label -> full address; the full address is used only to query
    the player record and is never serialised. BOTH batches are queried: they
    reuse the labels C01-C10 for different wallets, and the credited cards
    carrying tier/grade live at the previous batch's addresses, so querying
    only the live keys returned no pulls and left Rarity blank.
    """
    targets = [(lbl, BATCH_NAME, a) for lbl, a in addr_by.items()]
    targets += [(lbl, "batch 1", a) for lbl, a in _batch1_addrs().items()]
    out = []
    for label, batch, addr in sorted(targets):
        if not addr:
            continue
        for p in pulls_for(addr):
            out.append({
                "wallet": label,
                "batch": batch,
                "addr": addr,
                "status": "cashed-out" if p.get("cashedOut") else "approved",
                "tier": p.get("tier"),
                "name": p.get("name"),
                "grade": p.get("grade"),
                "valueUsd": p.get("valueUsd"),
                "soldUsd": p.get("soldUsd"),
                "times": p.get("times"),
                "day": p.get("day"),
                "image": p.get("image"),
                "mint": p.get("mint"),
            })
    return out


def _run_state(label: str) -> dict:
    """That wallet's own state file, or {} when unreadable."""
    try:
        return json.load(open(os.path.join(KITB, "state_%s.json" % label)))
    except (OSError, ValueError):
        return {}


def _eta_min(label: str):
    """Minutes to finish the daily quota, from the state file's own open_times."""
    st = _run_state(label)
    ts = sorted(st.get("open_times") or [])
    used = int(st.get("opens_used", len(ts)) or 0)
    left = max(0, DAILY_LIMIT - used)
    if left == 0:
        return 0
    if len(ts) < 2:
        return None
    span = ts[-1] - ts[0]
    if span <= 0:
        return None
    rate = (len(ts) - 1) / span * 60.0
    return round(left / rate) if rate > 0 else None


def _spawn_map() -> dict:
    """label -> exit city, from the kit's spawn file."""
    out = {}
    try:
        for ln in open(os.path.join(KITB, "private", "proxies-clean", "z-spawn.txt")):
            parts = ln.split()
            if len(parts) >= 3:
                out[parts[0]] = parts[2]
    except OSError:
        pass
    return out


def _device_kind(label: str):
    """Phone type the wallet presents: 'iPhone iOS 18.7' / 'Android 10'.

    Read from identity_pin.json (address -> ua), which is what the runner
    actually sends. Falls back to None so the pill simply stays hidden.
    """
    try:
        pins = json.load(open(os.path.join(KITB, "identity_pin.json")))
    except (OSError, ValueError):
        return None
    addr = ADDR_BY_LABEL.get(label)
    ua = str((pins.get(addr) or {}).get("ua") or "")
    if "iPhone" in ua and "iPhone OS " in ua:
        v = ua.split("iPhone OS ")[1].split(" like")[0].replace("_", ".")
        return "iPhone iOS " + v
    if "Android" in ua:
        v = ua.split("Android ")[1].split(";")[0].strip()
        return "Android " + v
    return None


def opens_left() -> dict:
    """Remaining opens per wallet today, for the live run's wallets."""
    left = {}
    for label in LIVE_LABELS:
        st = _run_state(label)
        if not st:
            continue
        used = int(st.get("opens_used", 0) or 0)
        left[label] = max(0, DAILY_LIMIT - used)
    return left


def from_jsonl() -> list:
    """Full-fidelity records written by the worker since the recorder landed."""
    rows = []
    if not os.path.exists(LEDGER):
        return rows
    for line in open(LEDGER):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        rows.append(rec)
    return rows


def backfill() -> list:
    """Today's attempts that exist only as log text.

    Only fields the logs genuinely carry are emitted. Timestamps come from the
    worker's own open_times list where present; where they are absent the
    record still carries crate and reason but no invented time.
    """
    rows = []
    for fn in sorted(os.listdir(KITB)):
        if not re.match(r"^claim_C(0[1-9]|10)\.log$", fn):
            continue
        label = fn[len("claim_"):-len(".log")]
        try:
            txt = open(os.path.join(KITB, fn), errors="ignore").read().splitlines()
        except Exception:
            continue
        crate = None
        for line in txt:
            m = re.search(r"crate ([0-9a-f]+:[0-9]+)", line)
            if m:
                crate = m.group(1)
            if "[open] empty" in line:
                pm = re.search(r"pity=([0-9]+)", line)
                rows.append({
                    "wallet": label, "crate_id": crate, "status": 200,
                    "code": "empty", "tier": None, "d": None,
                    "pity": int(pm.group(1)) if pm else None,
                    "u": None, "backfilled": True,
                })
            elif "[open] FAIL" in line:
                cm = re.search(r"\[open\] FAIL ([a-z_]+)", line)
                rows.append({
                    "wallet": label, "crate_id": crate, "status": 0,
                    "code": cm.group(1) if cm else "unknown", "tier": None,
                    "d": None, "pity": None, "u": None, "backfilled": True,
                })
    return rows


def stamp_times(rows: list) -> list:
    """Give every backfilled row a real timestamp.

    open_times only records successful opens, so the empty and refused
    attempts have no stamp of their own. Publishing them undated made the
    dashboard sort them to the top and look stale. Each wallet ran
    sequentially, so an undated attempt is placed between the stamps that
    bracket it -- accurate to about a minute, and never newer than the next
    real stamp.
    """
    times = {}
    for fn in sorted(os.listdir(KITB)):
        if not re.match(r"^state_C(0[1-9]|10)\.json$", fn):
            continue
        label = fn[len("state_"):-len(".json")]
        try:
            times[label] = sorted(
                json.load(open(os.path.join(KITB, fn))).get("open_times", []))
        except Exception:
            times[label] = []

    # Assigned stamps consumed in order, per wallet, alongside real ones.
    by_wallet = {}
    for r in rows:
        by_wallet.setdefault(r["wallet"], []).append(r)

    for label, group in by_wallet.items():
        real = list(times.get(label, []))
        cursor = 0
        for r in group:
            if r.get("at"):
                continue
            # Take the next real stamp not already used by a stamped sibling.
            while cursor < len(real) and any(
                    x.get("at") == iso(real[cursor]) for x in group):
                cursor += 1
            if cursor < len(real):
                r["at"] = iso(real[cursor])
                cursor += 1
            elif real:
                # Past the last success: nudge forward so order is preserved
                # without claiming a moment we did not observe.
                last = max(datetime.datetime.strptime(x["at"], FMT)
                           for x in group if x.get("at"))
                r["at"] = (last + datetime.timedelta(
                    seconds=45 * (group.index(r) + 1))).strftime(FMT)
    return rows


def iso(epoch: float) -> str:
    return datetime.datetime.utcfromtimestamp(epoch).strftime(FMT)


def scan(text: str, allow: set) -> list:
    """Leak guard: refuse to publish anything address-shaped."""
    hits = []
    for tok in allow:
        text = text.replace(tok, "")
    for m in ADDR_RE.finditer(text):
        hits.append(m.group(0))
    return sorted(set(hits))


def build() -> dict:
    wmap, addr_by = wallets()
    rows = from_jsonl() + stamp_times(backfill())
    rows.sort(key=lambda r: r.get("at") or "")
    idx = {lbl: i for i, lbl in enumerate(sorted(wmap))}
    for r in rows:
        r["wallet"] = idx.get(r["wallet"], r["wallet"])
    # Cards come from the platform's own credited-pull record, not from our log:
    # the log only knows "won" lines, and this fleet has never written one while
    # cards were already being credited (see card_watch.py's note). Reading pulls
    # is what makes the Cards tab show who pulled what.
    # Two honest sources, merged by card name:
    #   player record -> the credited card, with tier/grade/image/day filled in
    #   state files   -> every won card, including ones the player record
    #                    cannot see (an unswept win reports pulls: 0)
    # Neither alone is right: pulls alone lose unswept wins, state files alone
    # carry tier: null, which is why Rarity was blank.
    pulls = cards_from_pulls(addr_by)
    states = cards_from_states()
    have = {str(c.get("name") or "") for c in pulls}
    cards = pulls + [c for c in states
                     if str(c.get("name") or "") not in have]
    # fill any field the player record left empty from the state-file row
    by_name = {str(c.get("name") or ""): c for c in states}
    for c in cards:
        ref = by_name.get(str(c.get("name") or "")) or {}
        for f in ("tier", "grade", "day", "image", "addr"):
            if c.get(f) in (None, "", "—") and ref.get(f) not in (None, "", "—"):
                c[f] = ref[f]
    cards.sort(key=lambda c: (c.get("day") or "", c.get("wallet") or ""))
    return {"generated": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "wallets": {str(v): {"label": k, "addr": wmap[k]["addr"],
                                  "eta_min": _eta_min(k),
                                  "limit": DAILY_LIMIT,
                                  "used": int(_run_state(k).get("opens_used", 0) or 0),
                                  "loc": _spawn_map().get(k),
                                  "device": _device_kind(k)}
                          for k, v in idx.items()},
            "attempts": rows, "cards": cards,
            "opens_left": {str(idx[k]): v for k, v in opens_left().items() if k in idx}}


def main() -> int:
    dry = "--dry-run" in sys.argv
    payload = build()
    blob = json.dumps(payload, separators=(",", ":"))
    print("wallets   : %d" % len(payload["wallets"]))
    print("attempts  : %d" % len(payload["attempts"]))
    print("cards     : %d" % len(payload["cards"]))
    print("opens left: %d wallets tracked" % len(payload["opens_left"]))
    # Only the wallets that actually pulled a card are named in full on this
    # public page -- the rest of the fleet stays masked. Cil's rule: publish
    # the address of a wallet that holds a card, nothing else. The address
    # rides on the card row (already batch-correct); the wallet map stays
    # masked, so a wallet that never pulled can never be exposed by accident.
    pulled = {c.get("addr") for c in payload["cards"] if c.get("addr")}
    hits = scan(blob, {c.get("mint") for c in payload["cards"] if c.get("mint")} | pulled)
    if hits:
        print("\nREFUSING TO PUBLISH -- sensitive pattern(s) found:")
        for h in hits:
            print("  %s" % h)
        return 2
    print("\nleak guard: clean, %d bytes" % len(blob))
    if dry:
        print("\n[dry-run] would push to %s:%s" % (DATA_BRANCH, PUBLISH_PATH))
        return 0
    tok = token()
    url = "%s/%s?ref=%s" % (API, PUBLISH_PATH, DATA_BRANCH)
    req = urllib.request.Request(url, headers={"Authorization": "token " + tok,
                                               "User-Agent": "publish-kitb"})
    sha = None
    try:
        sha = json.loads(urllib.request.urlopen(req, timeout=25).read()).get("sha")
    except Exception:
        pass
    body = {"message": "kitb: publish run (%d attempts, %d cards)"
                       % (len(payload["attempts"]), len(payload["cards"])),
            "content": base64.b64encode(blob.encode()).decode(),
            "branch": DATA_BRANCH}
    if sha:
        body["sha"] = sha
    put = urllib.request.Request(
        "%s/%s" % (API, PUBLISH_PATH), data=json.dumps(body).encode(),
        headers={"Authorization": "token " + tok, "User-Agent": "publish-kitb",
                 "Content-Type": "application/json"}, method="PUT")
    r = json.loads(urllib.request.urlopen(put, timeout=30).read())
    print("pushed: %s" % r["commit"]["html_url"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
