# devops-feed-curator

A small self-hosted web page with three feeds:

| Page | What it shows |
|---|---|
| **Security** (`/`) | Critical and high-severity vulnerabilities and incidents for the products you run, listed in [`app/stack.yaml`](app/stack.yaml). Critical items are red and always at the top; high ones are orange, below them. |
| **Homelab Deals** (`/deals`) | Cheap mini PCs on eBay (FR/DE/UK) that make good homelab nodes: ThinkCentre Tiny, EliteDesk/ProDesk Mini, NUC, OptiPlex Micro. |
| **RAM Deals** (`/ram`) | DDR4/DDR5 sticks and kits on eBay. DDR3 and older are dropped. |

Each deals page has a "beyond budget" version (`/deals/beyond`, `/ram/beyond`) for listings above the main price limit.

## Quick start

```bash
cp .env.example .env      # set ONE_MIN_API_KEY (that's all the Security page needs)
docker compose up -d --build
```

Then open `http://<host>:8085`.

The deals pages also need eBay API keys (`EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET`). You can get them for free at [developer.ebay.com](https://developer.ebay.com). If you leave them blank, the deals pages are turned off and nothing else is affected.

**To upgrade:** run `git pull && docker compose up -d --build`. The database migrates itself.

## How it works

### Security page

Every hour (`NEWS_POLL_MINUTES`), the app checks two kinds of sources:

1. **CVE databases: [CISA KEV](https://www.cisa.gov/known-exploited-vulnerabilities-catalog) (actively exploited vulnerabilities) and [NVD](https://nvd.nist.gov) (all CVEs).** These are matched against `stack.yaml` with fixed rules. No LLM is involved, so they still work when the LLM is down:

   | Colour | Rule |
   |---|---|
   | 🔴 Critical | On CISA KEV and in your stack, or CVSS 9.0 or higher in your stack |
   | 🟠 High | CVSS 7.0 to 8.9 in your stack, or on CISA KEV but not in your stack |

   NVD is checked every 2 hours (its usage guidelines ask for no more), and fetched incrementally: every CVE changed since the last run, so a CVE that gets its score late is still picked up. If a High CVE later turns Critical or is added to KEV, it goes back to the top of the page. The score used is the highest of NVD's and the vendor's (CVSS v3.0, v3.1 and v4.0). The vendor's score usually arrives days before NVD's.
2. **Security news and vendor feeds** ([`app/sources.yaml`](app/sources.yaml)): Node.js and PHP security releases, Oracle patch updates, CISA advisories, BleepingComputer, SANS ISC and others. These give early warnings that CVE databases don't have yet. An LLM reads each new item and labels it critical, high or none, based on your stack. Items labelled none are dropped.

The top of the page shows whether CISA KEV and NVD are working. A red banner lists every source whose last fetch failed, so a dead feed can't look like a quiet week.

**Editing your stack:** each product in `stack.yaml` has NVD product IDs (`cpe`, exact) and description patterns (`keywords`, which catch CVEs NVD hasn't analyzed yet). Changes apply on the next poll. The file explains each field.

### Deals pages and the LLM

1. Once a day (at `POLL_HOUR`, midnight by default), the app runs the eBay searches.
2. New items go to an LLM (`gpt-4o-mini` via [1min.ai](https://1min.ai)), which decides which ones to keep and tags them.
3. Kept items are saved in SQLite (`data/feeds.db`) and shown on the pages.

If the main model fails, the app sends it one harmless test item. If that fails too, the model is down: the app skips it for an hour and uses `MODEL_CLASSIFIER_FALLBACK` instead (Claude Haiku by default, a different provider so one outage can't take out both). If every model is down, items wait until the next poll; they are never dropped because of an outage. If the test item works, one item in the batch is the problem (usually 1min.ai's content filter), so the app splits the batch to find it and drops only that item. Fallback use shows up in the logs: `docker compose logs | grep fallback`.

The Security page shows what was flagged in the last 7 days (`NEWS_MAX_AGE_DAYS`).

On the deals pages, prices are converted to EUR with shipping included. UK prices use the day's ECB exchange rate (from [frankfurter.dev](https://frankfurter.dev)), or `GBP_TO_EUR_RATE` if that fetch fails. Listings are sorted into three columns: cheapest, mid-range and priciest. Before the LLM sees anything, some listings are removed by fixed rules:
- broken or "for parts" listings
- listings that don't ship to France
- CPUs too old or weak for homelab use (see the table in [`app/ebay.py`](app/ebay.py))

Listings that have sold or ended are checked again and removed over time.

## Configuration

Everything is set in `.env`. [`.env.example`](.env.example) explains each setting. The ones you'll most likely change:

| Variable | Default | Purpose |
|---|---|---|
| `ONE_MIN_API_KEY` | — | Required. LLM API key. |
| `MODEL_CLASSIFIER` / `MODEL_CLASSIFIER_FALLBACK` | `gpt-4o-mini` / `us.anthropic.claude-haiku-5-5` | Main LLM and the backup used when it's down. Leave the fallback blank to turn it off. |
| `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` | — | Turn on the deals pages. |
| `EBAY_MARKETPLACES` | `EBAY_FR,EBAY_DE,EBAY_GB` | Which eBay sites to search. |
| `EBAY_DELIVERY_COUNTRY` | `FR` | Only show listings that ship here. |
| `DEAL_MAX_PRICE_EUR` / `DEAL_EXTENDED_MAX_PRICE_EUR` | `500` / `1500` | Mini PC price limits: main page / "beyond" page. |
| `RAM_MAX_PRICE_EUR` / `RAM_EXTENDED_MAX_PRICE_EUR` | `200` / `500` | RAM price limits: main page / "beyond" page. |
| `NEWS_POLL_MINUTES` | `60` | How often the Security page's sources are checked. |
| `NVD_API_KEY` | — | Optional, free. Speeds up large NVD fetches. |
| `POLL_HOUR` / `POLL_MINUTE` | `0` / `0` | When the daily deals fetch runs. |
| `ADMIN_TOKEN` | — | Lets you skip the refresh cooldown (see below). |

## Customizing

These files are mounted into the container. After you edit one, the change applies on the next poll. Prompt changes need a `docker compose restart`.

| To change… | Edit |
|---|---|
| The products you watch | [`app/stack.yaml`](app/stack.yaml) |
| Security news sources | [`app/sources.yaml`](app/sources.yaml) |
| eBay searches | [`app/deals.yaml`](app/deals.yaml), [`app/ram_deals.yaml`](app/ram_deals.yaml) |
| What the LLM keeps or drops | [`app/prompts/`](app/prompts/) (one prompt per page) |

**Reddit note:** Reddit blocks repeated anonymous requests, so all subreddits are fetched in a single combined URL (`www.reddit.com/r/sub1+sub2+.../new/.rss`), at most once every `REDDIT_POLL_HOURS` (8 by default). To add a subreddit, add it to that URL. Don't add a separate feed entry for it.

## Refreshing

- The **Refresh now** button on each page is safe to expose publicly. After one use, it is disabled for 10 minutes (`REFRESH_COOLDOWN_MINUTES`) so nobody can run up LLM costs. The refresh runs in the background, so reload the page after a minute.
- To refresh without waiting for the cooldown:
  ```bash
  curl -X POST -H "X-Admin-Token: <ADMIN_TOKEN>" https://<your-domain>/refresh
  ```
- To re-check all stored deals after changing the filtering logic, clear them. The next poll fetches them again:
  ```bash
  docker compose exec feed-curator python reset_deals.py
  ```
- To re-check news that was dropped by mistake (after an outage or a prompt change), put the last few days of dropped news back in the queue, then refresh:
  ```bash
  docker compose exec feed-curator python requeue_news.py 4
  ```

## Running tests

The tests in [`tests/`](tests/) cover the security rules (stack matching, KEV/NVD parsing, severity, merging, page order) and the eBay filtering rules. They need no API keys and make no network calls. You need Python 3.10 or newer:

```bash
pip install -r app/requirements.txt pytest
pytest
```

Or run them in Docker, without installing anything locally:

```bash
docker run --rm -v "$PWD":/repo -w /repo python:3.12-slim \
  sh -c "pip install -q -r app/requirements.txt pytest && pytest"
```

## Deploying publicly

The page has no login (it only links to public content). Put it behind an HTTPS reverse proxy. [`deploy/`](deploy/) has an nginx config and step-by-step instructions. The nginx config expects the app on port `8085`. If you change the port in `docker-compose.yml`, change it there too.
