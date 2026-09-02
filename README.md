# Bellhaven CRM sync

Reconciles the Bellhaven Senior Living website against the CRM, proposes the
changes that would bring the CRM back in line, and writes only what a human
approves.

**No dependencies.** Python 3.9+ and the standard library. No `pip install`, no
virtualenv, no API keys beyond the CRM token.

```bash
export BELLHAVEN_TOKEN=bh_...        # Windows: set BELLHAVEN_TOKEN=bh_...
#   ...or write the token into token.txt in the project root (gitignored)

python run.py daily                  # fetch the site + CRM, generate proposals
python run.py review                 # review app on http://127.0.0.1:8787
#   ... approve / reject in the browser, then hit "Apply approved to CRM"

python -m unittest discover -s tests # 29 tests, ~0.1s
```

## The four stages

| Stage | Command | Network | Writes |
|---|---|---|---|
| fetch | `run.py fetch` | website + CRM (read) | `data/raw/`, `data/snapshot/` |
| pipeline | `run.py pipeline` | none | proposals in `data/bellhaven.sqlite3` |
| review | `run.py review` | none | decisions in SQLite |
| apply | `run.py apply` | CRM (write) | the CRM |

They are separate on purpose. `pipeline` is a pure function of the snapshot, so
any run can be reproduced from `data/snapshot/` months later. `apply` can only
execute proposals whose status is already `approved`, so there is no path from
"the scraper saw something odd" to "the CRM changed" that does not pass through
a person.

Useful flags:

```bash
python run.py fetch --offline        # re-parse data/raw/ without touching the network
python run.py apply --dry-run        # print the exact API calls, send nothing
python run.py apply --plan-out p.json    # write the resolved calls to a file
python run.py ingest-results r.json      # record results of a plan applied elsewhere
python run.py decide <fingerprint> approve|reject --note "..."
python run.py status
```

`--plan-out` / `ingest-results` exist because approvals and network access do not
always live in the same place. They were also how this submission's writes were
executed (see WRITEUP.md, "How this was run").

## Layout

```
run.py                  CLI
bellhaven/
  config.py             thresholds, URLs, token, matching profile
  httpc.py              urllib wrapper: retries, no retry on deterministic 4xx
  scraper.py            website -> Location records (+ announcements, acquisitions)
  crm.py                CRM client; refuses to PATCH a field outside a whitelist
  normalize.py          address + name canonicalisation
  match.py              scoring, clustering, survivor selection
  propose.py            proposals, including the CHOW SOP
  store.py              SQLite: proposals, decisions, vetoes, created accounts
  pipeline.py           stage orchestration
  review_app.py         local approval UI (http.server, single file)
ops/
  crontab.txt                          daily schedule
  .github/workflows/daily-sync.yml     same thing as a GitHub Action
tests/test_pipeline.py  29 unit tests
data/
  raw/                  exact bytes fetched (replay + parser tests)
  snapshot/             parsed website + CRM state per run
  bellhaven.sqlite3     the queue
```

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `BELLHAVEN_TOKEN` | — (required) | CRM bearer token. Falls back to a gitignored `token.txt`; never committed. |
| `BELLHAVEN_PROFILE` | `conservative` | `conservative` (auto-confident at 90) or `balanced` (78) |
| `BELLHAVEN_SITE` / `BELLHAVEN_API` | Railway host | endpoints |
| `BELLHAVEN_REVIEW_PORT` | `8787` | review app port |
| `BELLHAVEN_DATA` | `./data` | state directory |
