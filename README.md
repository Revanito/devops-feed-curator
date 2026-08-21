# devops-feed-curator

A small self-hosted webpage that pulls IT/DevOps/homelab/IT-news RSS feeds (Reddit, Hacker News, and a
handful of blogs and security/news sites) and uses an LLM to filter out the noise — deal posts, "what
should I buy" threads, off-topic gadget content — keeping only substantive self-hosting/DevOps/homelab
items and admin-relevant IT world news.

## How it works

- `app/sources.yaml` lists the feeds to poll (reddit subs, HN, blogs, IT-news sites like BleepingComputer,
  Krebs on Security, SANS ISC, and the Microsoft Security Blog). It's volume-mounted into the container,
  so edits take effect on the next poll cycle without a rebuild.
- Each item's summary has HTML tags stripped and entities decoded *before* truncating to length, not
  after — truncating raw HTML first can cut mid-tag and leave malformed markup that neither displays
  cleanly nor sends to the classifier cleanly (this was the actual cause of a full outage once: 1min.ai
  was returning an immediate `status: FAILURE` on every batch that included one of these malformed
  summaries, so nothing got classified for days).
- Every `POLL_INTERVAL_MINUTES` (default 60), the app fetches all feeds, stores new items in a local
  SQLite DB (`data/feeds.db`), then sends unclassified items in batches to a cheap LLM (`gpt-4o-mini` via
  1min.ai, same API key/provider as [discord-1min-proxy](../discord-1min-proxy)) asking it to keep/drop
  each one, tag it with a topic, and flag it `critical` if it's a big, broadly-impactful incident (major
  outages, actively-exploited zero-days, CrowdStrike-style events) rather than routine news.
- Raw feeds (especially Hacker News, which mirrors everything submitted, not just tech) occasionally
  include an item whose content trips 1min.ai's own moderation and gets the *entire batch* rejected with
  no per-item detail. If a batch fails, the app automatically bisects it and retries the halves, isolating
  which single item is the problem. A lone item is retried once more before being quarantined (marked
  classified, dropped, tagged `unclassifiable`) — this avoids permanently dropping a perfectly fine item
  that just happened to hit a transient API blip while alone in a bisected batch of one.
- The webpage at `/` shows the kept items in four columns — Reddit, Blogs & DevOps News, Homelab
  (homelab-tagged items win that column regardless of source), and Homelab Deals — newest first within
  each. Admin-relevant IT world news lands in Blogs & DevOps News, tagged accordingly (e.g. `microsoft`,
  `outage`, `security`).
- **Homelab Deals** is a separate pipeline from the RSS/news one: `app/ebay.py` polls the eBay Browse API
  (searches defined in `app/deals.yaml` - ThinkCentre Tiny, EliteDesk Mini, ProDesk Mini, NUC, OptiPlex
  Micro) across the marketplaces in `EBAY_MARKETPLACES` (default `EBAY_FR,EBAY_DE,EBAY_GB` - Germany and
  the UK both have far more listings for this category than France alone), pre-filtered server-side to
  `DEAL_MAX_PRICE_EUR` (default €500) then re-checked in `ebay.py` against price **+ shipping**, converted
  to EUR, since the search API's own filter only looks at the item price and can't filter across
  currencies. GB listings are priced in GBP; `GBP_TO_EUR_RATE` (default 1.17, not live-fetched - update it
  by hand occasionally) converts them so the ceiling and the price badge both mean the same thing
  regardless of which marketplace a listing came from. The price badge shown on each card is always the
  EUR-equivalent item + shipping total; the original native amount stays visible in the card's summary
  text for transparency. `EBAY_GB` listings are also flagged in that text as cross-border (post-Brexit) -
  the buyer may owe import VAT/duty on delivery that eBay's search API has no reliable way to quote up
  front, so that's a warning label rather than a computed number.
  Results are stored the same way as feed items (`kind = 'deal'`) but classified with a *different* system
  prompt (`classifier.py` `_DEALS_SYSTEM_PROMPT`) that judges the actual hardware match - genuine
  mini/micro/tiny form factor, Intel i7/i9 8th-gen-or-newer (or AMD Ryzen 5/7/9 3000-series-or-newer) -
  since these platforms are DDR4 by default from 2018 onward, RAM upgradability isn't used as a hard filter
  even when a listing doesn't state 32GB installed. The same prompt also extracts CPU (with core/thread
  count filled in from the model's known specs), RAM, and storage from the listing title, shown as spec
  badges on each card - eBay's search API doesn't return structured item aspects, so this relies on the
  seller having put specs in the title, which is near-universal for this listing category. For "choose
  your configuration" listings (one listing, several selectable RAM/storage combos behind a dropdown -
  common among bulk refurb sellers), `ebay.py` makes one extra API call per unique listing to fetch every
  variation's real price and options, so the classifier can report an accurate range (e.g. "8-32GB")
  instead of just whatever the cheapest configuration happens to be. Leave
  `EBAY_CLIENT_ID`/`EBAY_CLIENT_SECRET` blank in `.env` to disable this column entirely (get free keys at
  [developer.ebay.com](https://developer.ebay.com) -> create an app -> production keys).
- Anything flagged `critical` also shows in a **Must Read** banner above the three columns (capped at 8,
  newest first), so a major incident doesn't get lost in the normal flow. Items older than 7 days drop
  out of Must Read automatically (they stay in their normal column, just not the banner).
- A **Latest CVEs** section sits below Must Read, above the columns: the 3 most recent items tagged `cve`
  (genuine vulnerability disclosures on major products - Microsoft, Linux, nginx, Apache, Docker,
  Kubernetes, etc, as opposed to generic `security` news). No dedicated CVE feed needed - it's just a
  display pull from whatever the classifier already tagged that way.
- There's a "Refresh now" button, safe to expose publicly: it's rate-limited to one manual poll per
  `REFRESH_COOLDOWN_MINUTES` (default 10) so a shared link can't be used to spam paid LLM calls. It greys
  out and reads "cooling down" while a recent refresh is still in effect.
- Clicking it returns instantly (redirects straight back to `/`) - the actual feed fetch + classification
  runs in the background rather than blocking the request, so it doesn't sit there long enough to trip a
  reverse-proxy timeout (this used to 502 after ~60s on a slow poll, then recover once the request finally
  finished server-side). A lock prevents two polls (manual + the hourly schedule) from ever running at once.
  The page just won't show fresh results until the background poll actually finishes - reload after a bit.

## Setup

1. `cp .env.example .env` and fill in `ONE_MIN_API_KEY` (the same key used by discord-1min-proxy).
2. `docker compose up -d --build`
3. Open `http://<host>:8085`

Upgrading an existing deployment (e.g. after pulling the `critical`/Must Read change) is just
`git pull && docker compose up -d --build` — the SQLite schema migrates itself on startup, no manual
steps or data loss.

## Tuning the filter

The keep/drop rules live in `app/classifier.py` (`_SYSTEM_PROMPT` for news, `_DEALS_SYSTEM_PROMPT` for the
Homelab Deals column). Edit either prompt directly if the filter is too strict or too loose, then rebuild
(`docker compose up -d --build`) — no code changes needed elsewhere.

## Adding/removing feeds

Edit `app/sources.yaml`. Any standard RSS/Atom feed URL works. Reddit subreddits: append `/.rss` to the
subreddit URL. No restart required — the file is volume-mounted and read fresh on each poll.

## Adding/removing eBay deal searches

Edit `app/deals.yaml` — each entry is just a `name` and an eBay `keywords` search string, run against every
marketplace in `EBAY_MARKETPLACES`. Volume-mounted like `sources.yaml`, so no rebuild needed.

## Forcing a refresh, bypassing the cooldown

Set `ADMIN_TOKEN` in `.env` to any random string, then:

```
curl -X POST -H "X-Admin-Token: <your token>" https://feeds.example.com/refresh
```

This always runs immediately regardless of `REFRESH_COOLDOWN_MINUTES`. Leave `ADMIN_TOKEN` blank (the
default) if you don't need that — the public button still works, just subject to the cooldown.

## Exposing it publicly

Deployed at feeds.example.com, etc. — public HTTPS traffic hits a reverse-proxy nginx that terminates TLS
and `proxy_pass`es to this container. The page needs no auth (it's just links out to public articles);
the refresh button is cost-bounded by the cooldown above rather than gated behind auth. See
[`deploy/`](deploy/) for the nginx vhost and step-by-step setup.
