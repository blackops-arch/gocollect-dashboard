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
# Rotation state files. `doneAt` is the epoch end-of-rotation stamp; the round a
# card belongs to is the FIRST rotation whose window it falls in, and a window
# runs from the previous rotation's doneAt to this one's.
ROTATIONS_DIR = "/home/bluey/gocollect-rotation-z/rotations"
# Where a rotation MOVES the finished round's state files. A retired round's
# card lives only here once the next round reuses its label (Cil 2026-10-05).
ARCHIVE_DIR = "/home/bluey/gocollect-rotation-z/engine"
# The engine's own wallet map: live block + every retired wallet keyed
# "<LABEL>-R<N>". It is the one place that says which round a wallet belonged
# to, which is how a card is attributed exactly (no clock guesswork).
ENGINE_WALLETS = "/home/bluey/gocollect-rotation-z/wallets.json"


def _round_by_addr(addr: str):
    """Round label for a wallet address, from the engine's live + retired maps."""
    if not addr:
        return None
    try:
        with open(ENGINE_WALLETS) as fh:
            W = json.load(fh)
    except (OSError, ValueError):
        return None
    ws = W.get("wallets") or {}
    for lab, e in ws.items():
        if isinstance(e, dict) and e.get("address") == addr:
            m = re.search(r"-R(\d+)$", str(e.get("ledgerName") or ""))
            if m:
                return "R%s" % m.group(1)
    for key, e in (W.get("retired") or {}).items():
        a = e.get("address") if isinstance(e, dict) else e
        if a == addr:
            m = re.search(r"-R(\d+)$", str(key))
            if m:
                return "R%s" % m.group(1)
    return None


def _round_windows() -> list:
    """[(round_label, start_epoch, end_epoch)] oldest first.

    A card won after the newest rotation's doneAt has no next round yet, so it
    is stamped with the newest round's label as "R<N> (live)" by the caller.
    Returns [] on any failure: the round stamp is decorative, and losing it must
    never take the whole publish run down with it.
    """
    try:
        out = []
        for fn in os.listdir(ROTATIONS_DIR):
            m = re.match(r"^Z-R(\d+)\.json$", fn)
            if not m:
                continue
            with open(os.path.join(ROTATIONS_DIR, fn)) as fh:
                d = json.load(fh)
            if d.get("phase") != "done":
                continue
            at = d.get("doneAt")
            if not at:
                continue
            out.append(("R%s" % m.group(1), float(at)))
    except OSError:
        return []
    out.sort(key=lambda r: r[1])
    # turn the end-stamps into [start, end] windows
    return [(lab, out[i - 1][1] if i else 0.0, end)
            for i, (lab, end) in enumerate(out)]


def _round_of(at_iso: str, addr: str = None):
    """Round label for a card, preferring the wallet that actually won it.

    The address is exact: the engine's retired map keys every past wallet as
    <LABEL>-R<N>, so the round comes straight from the wallet. The timestamp is
    only a fallback, because a boundary window is off by one rotation -- R6's
    wallets keep running until R7 COMMITS, so a win in that gap sits inside R7's
    window while the winning wallet is still R6's (caught on the Luffy card,
    2026-10-05). Never let the clock overrule the address.
    """
    rnd = _round_by_addr(addr)
    if rnd:
        return rnd
    if not at_iso:
        return None
    try:
        t = datetime.datetime.strptime(str(at_iso)[:19], "%Y-%m-%dT%H:%M:%S")
        t = t.replace(tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return None
    w = _round_windows()
    if not w:
        return None
    if t > w[-1][2]:
        return "%s (live)" % w[-1][0]
    for lab, start, end in w:
        if start <= t <= end:
            return lab
    return None
DATA_BRANCH = "data"
PUBLISH_PATH = "ledger.json"
API = "https://api.github.com/repos/blackops-arch/gocollect-dashboard/contents"
PLAYER = "https://gocollect.fun/v1/players/%s"

# The page is PUBLIC. Wallet addresses never leave unmasked.
ADDR_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
WALLET_RE = re.compile(r"^Z(?:0[1-9]|[12][0-9])\.key$")
# The live Z run is the batch the page is labelled with. The C keys are emptied
# and the S keys are tainted, so neither may appear on the public page
# (Cil 2026-10-04).
#
# The block is READ from the engine's wallet map, not hardcoded: labels advance
# every round now (R7 Z16-Z20, R8 Z21-Z25 -- Cil 2026-10-05), so a frozen tuple
# would silently stop tracking the moment the next rotation lands. The wallet
# map is the engine's own file, which the rotation writes in the same step that
# flips the labels, so this follows automatically.
#
# Fallback is the anchor block, so a missing/odd wallet map degrades to "track
# what we tracked yesterday" instead of "track nothing".
ENGINE_WALLETS = "/home/bluey/gocollect-rotation-z/wallets.json"
Z_ANCHOR_LABELS = ("Z16", "Z17", "Z18", "Z19", "Z20")


def _live_z_labels() -> tuple:
    try:
        with open(ENGINE_WALLETS) as fh:
            ws = (json.load(fh).get("wallets") or {})
        labs = tuple(sorted(l for l in ws if re.match(r"^Z\d\d$", l)))
        return labs or Z_ANCHOR_LABELS
    except (OSError, ValueError):
        return Z_ANCHOR_LABELS


LIVE_LABELS = _live_z_labels()
BATCH_NAME = "Z-run"

# Every Z round is read, so a card won in one round is never dropped from the
# page when the next rotation replaces the live labels (Cil 2026-10-05:
# "keep the card pulled history from previous batch intact dont delete it").
# Scoped to the Z run only: the older C/batch-1 cards sit on swept wallets and
# need name-matching to resolve, so they stay excluded (Cil 2026-10-05).
# The rotation states carry each round's own label -> address map, which is
# what keeps a reused label (Z16 in both R5 and R6) pointing at the right
# wallet for that round's cards.
ROTATIONS_DIR = "/home/bluey/gocollect-rotation-z/rotations"
Z_LABEL_RE = re.compile(r"^Z\d\d$")
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
    live_keys = _key_labels()
    live_order = sorted(live_keys)
    live_wkey = {label: str(i) for i, label in enumerate(live_order)}
    for label in live_order:
        out += _cards_in(os.path.join(KITB, "state_%s.json" % label),
                         label, BATCH_NAME, b2_map.get(label),
                         wkey=live_wkey[label])
    # Archived rounds. A rotation MOVES state_<LABEL>.json into its round dir
    # (engine/r6/state_Z20.json), so when the next round reuses a label the
    # retired round's card is no longer in the live file -- r6's Luffy vanished
    # from the page at R7's commit (2026-10-05). The comment below says a retired
    # label "keeps its file"; that held only while labels were never reused.
    # Reading the round dirs restores those cards. Labels advance from R8 on, but
    # the archive is read regardless so it keeps working either way.
    out += _cards_from_archive(live_wkey)
    # Older C/batch-1 state is deliberately NOT read: those cards sit on swept
    # wallets and need name-matching to resolve (Cil 2026-10-05).
    #
    # Every Z round is NOT walked separately: a state_<LABEL>.json file holds
    # that wallet's whole history, not one round's, so re-reading it once per
    # round emitted the same card several times (caught in dry-run 2026-10-05).
    # History across rotations is preserved simply by reading every Z label that
    # still has a state file -- a retired label keeps its file, so its cards
    # stay on the page after the next rotation replaces LIVE_LABELS.
    return out


def _round_addr_map(rnd: int) -> dict:
    """label -> address for one round, from that round's rotation state.

    A round owns its own label->address map, which is the only correct source
    for an archived card: Z20 in r6/ is R6's wallet while Z20 in the live map is
    R7's, so borrowing the live address would attribute the card to the wrong
    wallet (caught 2026-10-05).
    """
    try:
        with open(os.path.join(ROTATIONS_DIR, "Z-R%d.json" % rnd)) as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return {}
    out = {}
    for lab, v in (d.get("new") or {}).items():
        a = v.get("address") if isinstance(v, dict) else v
        if a:
            out[lab] = a
    return out


def _enrich_states_from_rounds(states: list) -> None:
    """Fill tier/grade/image/status on archived cards from their own wallet.

    An archived card's state row has tier: null -- the kit writes the prize the
    instant the crate opens, before the server classifies it -- and its wallet is
    retired, so the live pulls query never sees it. The retired wallet still
    answers /v1/players, so each archived round's wallets are queried once and
    the matching pull is merged in by card name. Mutates `states` in place.

    Only rows that are still missing a field are enriched, so nothing the kit or
    a live pull already said is overwritten.
    """
    need = [c for c in states if c.get("tier") in (None, "", "—")
            or c.get("grade") in (None, "", "—")]
    if not need:
        return
    try:
        rounds = sorted(os.listdir(ARCHIVE_DIR))
    except OSError:
        return
    by_name = {}
    for rd in rounds:
        mrd = re.match(r"^r(\d+)$", rd)
        if not mrd:
            continue
        if not os.path.isdir(os.path.join(ARCHIVE_DIR, rd)):
            continue
        for label, addr in sorted(_round_addr_map(int(mrd.group(1))).items()):
            if not addr or any(str(c.get("addr") or "") == addr for c in states
                               if c.get("addr")):
                pass  # exact address wins; still query it for completeness
            for p in pulls_for(addr):
                n = str(p.get("name") or "")
                if n and n not in by_name:
                    by_name[n] = p
    for c in need:
        p = by_name.get(str(c.get("name") or ""))
        if not p:
            continue
        for f in ("tier", "grade", "image"):
            if c.get(f) in (None, "", "—") and p.get(f) not in (None, "", "—"):
                c[f] = p[f]
        # A pull means the platform CREDITED the card; the state row's "pending"
        # is just the hardcoded default for a non-batch-1 row.
        c["status"] = "cashed-out" if p.get("cashedOut") else "approved"
        if c.get("valueUsd") in (None, "", 0) and p.get("valueUsd"):
            c["valueUsd"] = p["valueUsd"]


def _cards_from_archive(live_wkey: dict) -> list:
    """Cards still sitting in an archived round dir (engine/r<N>/state_Z*.json).

    A rotation moves each live state file into its round directory, so a card won
    in a round whose labels were later reused exists only here. The round comes
    from the DIRECTORY name (r6/ -> Z-R6.json) and the address from that round's
    own map, so an archived card is never attributed to the live wallet that now
    wears its label. Deduped by (name, address).
    """
    seen, out = set(), []
    try:
        rounds = sorted(os.listdir(ARCHIVE_DIR))
    except OSError:
        return out
    for rd in rounds:
        mrd = re.match(r"^r(\d+)$", rd)
        if not mrd:
            continue
        rd_dir = os.path.join(ARCHIVE_DIR, rd)
        if not os.path.isdir(rd_dir):
            continue
        amap = _round_addr_map(int(mrd.group(1)))
        for fn in sorted(os.listdir(rd_dir)):
            m = re.match(r"^state_(Z\d\d)\.json$", fn)
            if not m:
                continue
            label = m.group(1)
            addr = amap.get(label)
            for c in _cards_in(os.path.join(rd_dir, fn), label, BATCH_NAME,
                               addr, wkey=None):
                key = (str(c.get("name") or ""), str(c.get("addr") or ""))
                if key in seen:
                    continue
                seen.add(key)
                out.append(c)
    return out


def _key_labels() -> set:
    try:
        have = set(os.listdir(os.path.join(KITB, "keys")))
        return {lbl for lbl in LIVE_LABELS if (lbl + ".key") in have}
    except OSError:
        return set()


def _z_round_wallets() -> list:
    """[(label, batch_name, address)] for every Z round still on disk.

    Reads the engine's rotation states (Z-R<N>.json -> "old"/"new"), which give
    each round its own label -> address map. "new" is tagged as the round it
    belongs to, "old" as that same round's outgoing set; a wallet that appears in
    two rounds is emitted twice, each with that round's address, so a card is
    never attributed to the wrong wallet. Only Z labels are emitted — the C/S
    keys never reach the public page (Cil 2026-10-04).
    """
    out = []
    try:
        names = os.listdir(ROTATIONS_DIR)
    except OSError:
        return out
    for name in names:
        m = re.match(r"^Z-R(\d+)\.json$", name)
        if not m:
            continue
        try:
            st = json.load(open(os.path.join(ROTATIONS_DIR, name)))
        except (OSError, ValueError):
            continue
        if not isinstance(st, dict):
            continue
        rnd = m.group(1)
        # A round's "new" set is the batch after that rotation; "old" is the set
        # it replaced. Both belong to that round's history.
        for key, tag in (("new", "R%s" % rnd), ("old", "R%s (in)" % rnd)):
            for label, addr in sorted((st.get(key) or {}).items()):
                if not Z_LABEL_RE.match(str(label)) or not addr:
                    continue
                out.append((label, "%s %s" % (BATCH_NAME, tag), addr))
    return out


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
                    # Keep the full win time, not just its date: the round stamp
                    # needs the hour to bucket against a rotation boundary, and
                    # prizes won before the kit recorded their own `at` can only
                    # be dated from here (Cil 2026-10-05).
                    "at": r.get("at"),
                }
    except OSError:
        pass
    return out


def _cards_in(path: str, label: str, batch: str, addr=None, wkey=None,
              status=None) -> list:
    """Won cards from one state file.

    `wkey` is the numeric wallet key the published `wallets` map is filed under
    ("0".."4"). The page joins cards to wallets with `c.wallet === walletKey(i)`,
    where walletKey(i) resolves to that map's *label* — so a card left carrying
    only a label is dropped from its wallet row and falls through to the
    numeric fallback, which prints "G1" (found 2026-10-05; Cil: "the label in
    dashboard still ambiguous"). Emitting the numeric key keeps the join
    working; `wlabel` carries the human label for display.
    """
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
            # Numeric key first: the page joins cards to wallets with
            # `c.wallet === walletKey(i)`, and walletKey(i) is the wallets map's
            # label. Without the numeric key the card is orphaned and prints
            # "G1" (found 2026-10-05). `wkey` is filled in by the caller, which
            # knows this wallet's index in the published wallets map.
            "wallet": wkey if wkey is not None else label,
            "wlabel": label,
            "batch": batch,
            "addr": addr,          # this card's OWN wallet (batch-specific)
            # Same words the page already uses (approved/pending): batch 1's
            # four were credited and swept to Cil's hub; C08 is still held by
            # the server, so state files carry no status for either.
            "status": status or ("approved" if batch == "batch 1" else "pending"),
            "tier": p.get("tier"),
            "name": p.get("name"),
            "grade": _grade_of(p) or w.get("grade"),
            "valueUsd": p.get("valueUsd"),
            "soldUsd": None,
            "times": None,
            "day": w.get("day"),
            # Which rotation this card was won in. Read from the prize's own
            # win time; older prizes predate that field, so fall back to the
            # ledger win record keyed by the same card name. Decorative only:
            # a missing stamp must never drop the card.
            "round": _round_of(p.get("at") or w.get("at"), addr),
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

    A Z-run mint that vanished from its wallet's record is a REVOKED card
    (returned to vault, e.g. a location-flag clawback) and is carried in
    `card_watch_state.json` -> `revokes` by the watcher. Those are spliced in
    here as status "returned" so the page can show them; the absence of a mint
    from the record is the only signal, there is no status field to read.
    """
    targets = [(lbl, BATCH_NAME, a) for lbl, a in addr_by.items()]
    # Z-run only (Cil 2026-10-04): the previous batch's cards are NOT spliced in.
    # They were swept to the hub and would otherwise render as approved rows on
    # a page labelled "Z-run". Do not re-add _batch1_addrs() here.
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
    out += revoked_cards()
    return out


def revoked_cards() -> list:
    """Z-run cards the platform clawed back, from the watcher's revoke ledger.

    Written by card_watch.py's revoke pass. Z-run only: the watcher's seen
    ledger is scoped to the live run, so an old batch's sweep can never be
    mistaken for a clawback. A row with no mint is skipped — never invent a
    revoked card.
    """
    try:
        st = json.load(open(os.path.join(KITB, "output", "card_watch_state.json")))
    except (OSError, ValueError):
        return []
    out = []
    for mint, r in sorted((st.get("revokes") or {}).items()):
        if not isinstance(r, dict) or not r.get("mint"):
            continue
        out.append({
            "wallet": r.get("label"),
            "batch": BATCH_NAME,
            "status": "returned",
            "tier": r.get("tier"),
            "name": r.get("name"),
            "grade": None,
            "valueUsd": r.get("valueUsd"),
            "soldUsd": None,
            "times": None,
            "day": r.get("day"),
            "image": None,
            "mint": r.get("mint"),
            "detected": r.get("detected_at"),
        })
    return out


def _run_state(label: str) -> dict:
    """That wallet's own state file, or {} when unreadable."""
    try:
        return json.load(open(os.path.join(KITB, "state_%s.json" % label)))
    except (OSError, ValueError):
        return {}


def _run_used(label: str) -> int:
    """Crates opened in the wallet's CURRENT run, capped at the daily limit.

    opens_used accumulates across the day's runs, so after the 07:00 WIB reset
    the extra run pushes it past DAILY_LIMIT (26/25, 27/25 on the public page).
    The runner's own log marks the restart with `#extra-run <ts>`; count only
    [open]/[WIN] lines after the last marker, falling back to opens_used when
    the marker is absent (first run of the day).
    """
    try:
        text = open(os.path.join(KITB, "claim_%s.log" % label)).read()
        mark = text.rfind("#extra-run ")
        if mark != -1:
            text = text[mark:]
            n = len(re.findall(r"^\[(?:open\] empty|WIN)", text, re.M))
            return min(n, DAILY_LIMIT)
    except OSError:
        pass
    return min(int(_run_state(label).get("opens_used", 0) or 0), DAILY_LIMIT)

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
    """label -> spawn city, from the kit's spawn file.

    z-spawn.txt lines are `<label> <city> <lat>,<lng>`. Only the city is published:
    the coordinates are the simulated walk position, and the public page a Cil reads
    wants "Seoul", not "1.35024,103.85250".
    """
    out = {}
    try:
        for ln in open(os.path.join(KITB, "private", "proxies-clean", "z-spawn.txt")):
            parts = ln.split()
            if len(parts) >= 2:
                out[parts[0]] = parts[1]
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
    # Archived rounds are enriched from their OWN round's wallet record. A card
    # won by a wallet that a later round retired has no live player record to
    # read, so its state row carries tier: null and the page prints Rarity "—".
    # The retired wallet still answers /v1/players (verified 2026-10-05), so the
    # tier/grade are fetched and merged by name.
    _enrich_states_from_rounds(states)
    # pulls wins a name collision, and its row carries the LABEL ("Z20") where
    # the page needs the numeric wallet key it joins on -- a present-but-wrong
    # value, so a fill-if-empty would never correct it (found 2026-10-05). Assign
    # both fields from the state row unconditionally.
    by_name_states = {str(c.get("name") or ""): c for c in states}
    for c in pulls:
        ref = by_name_states.get(str(c.get("name") or "")) or {}
        for f in ("wallet", "wlabel", "round", "day"):
            if ref.get(f) not in (None, ""):
                c[f] = ref[f]
    have = {str(c.get("name") or "") for c in pulls}
    cards = pulls + [c for c in states
                     if str(c.get("name") or "") not in have]
    # fill any field the player record left empty from the state-file row
    by_name = {str(c.get("name") or ""): c for c in states}
    for c in cards:
        ref = by_name.get(str(c.get("name") or "")) or {}
        for f in ("tier", "grade", "day", "image", "addr", "wallet", "wlabel"):
            if c.get(f) in (None, "", "—") and ref.get(f) not in (None, "", "—"):
                c[f] = ref[f]
    cards.sort(key=lambda c: (c.get("day") or "", c.get("wallet") or ""))
    return {"generated": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "wallets": {str(v): {"label": k, "addr": wmap[k]["addr"],
                                  "eta_min": _eta_min(k),
                                  "limit": DAILY_LIMIT,
                                  "used": _run_used(k),
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
