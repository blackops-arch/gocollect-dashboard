# GoCollect fleet dashboard

A live, read-only view of the GoCollect farming fleet. Nothing here is a key,
an address, or log content — just counts and liveness.

## What's in here

| File | What it is |
|---|---|
| `index.html` | the dashboard page (static; GitHub Pages serves it) |
| `state.json` | the fleet snapshot the page reads — rewritten by the publisher |
| `publish_state.py` | writes `state.json` from the local dashboard and pushes it |

## How it works

```
host: farm fleet ──> public_dash.py (127.0.0.1:8790) ──> publish_state.py ──> state.json (this repo)
                                                                                     │
browser: index.html (GitHub Pages) ── reads state.json ──────────────────────────────┘
                                   └─ reads live odds/stats straight from gocollect.fun
```

* **Live game data** (free-crate odds, 24h platform cards/value) is fetched by the
  browser directly from `gocollect.fun`, which serves permissive CORS.
* **Fleet data** (opens/cards/pity/liveness per wallet) comes from `state.json`,
  refreshed by the publisher.

## The publisher

Runs on the host, reads the sanitized local endpoint, and refuses to publish if
the payload contains anything address-like, IP-like, or proxy-host-like:

```
python3 publish_state.py --dry-run   # check what would go out
python3 publish_state.py             # write + commit + push
```

Scheduled by `gocollect-dash-publish.timer` (every minute).

## Privacy

The page shows per-wallet **counts** only — no addresses, no keys, no proxy
endpoints, no log lines. `publish_state.py` enforces that on every push.
