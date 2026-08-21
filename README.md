# devops-feed-curator

A small self-hosted webpage with two LLM-curated feeds.

**News.** Pulls IT/DevOps/homelab/IT-news RSS feeds (Reddit, Hacker News, and a handful of blogs and
security/news sites) and filters out the noise — shopping/deal posts, "what should I buy" threads,
off-topic gadget content — keeping only substantive self-hosting/DevOps/homelab items and admin-relevant
IT world news.

**Homelab Deals.** Does the opposite for actual hardware: searches eBay (France, Germany, UK) for cheap
small-form-factor PCs — Lenovo ThinkCentre Tiny, HP EliteDesk/ProDesk Mini, Intel NUC, Dell OptiPlex Micro
— worth building a homelab node out of, and applies the same LLM-judged approach to keep only genuine
matches: real mini-PC form factor, modern-enough CPU generation, not just a keyword hit on the title.

## How it works

Three pages, sharing one poll/classify cycle and one nav bar (top-right, on every page): **News** (`/`),
**Homelab Deals** (`/deals`), and **Homelab Deals beyond €500** (`/deals/beyond`).

### News feed (`/`)

- `app/sources.yaml` lists the feeds to poll (reddit subs, HN, blogs, IT-news sites like BleepingComputer,
  Krebs on Security, SANS ISC, and the Microsoft Security Blog). It's volume-mounted into the container,
  so edits take effect on the next poll cycle without a rebuild.
- Every `POLL_INTERVAL_MINUTES` (default 60), the app fetches all feeds, stores new items in a local
  SQLite DB (`data/feeds.db`), then sends unclassified items in batches to a cheap LLM (`gpt-4o-mini` via
  1min.ai, same API key/provider as [discord-1min-proxy](../discord-1min-proxy)) asking it to keep/drop
  each one, tag it with a topic, and flag it `critical` if it's a big, broadly-impactful incident (major
  outages, actively-exploited zero-days, CrowdStrike-style events) rather than routine news.
- Kept items land in three columns — Reddit, Blogs & DevOps News, and Homelab (homelab-tagged items win
  that column regardless of source) — newest first within each. Admin-relevant IT world news lands in
  Blogs & DevOps News, tagged accordingly (e.g. `microsoft`, `outage`, `security`).
- A **Must Read** banner sits above the columns for anything flagged `critical` (capped at 8, newest
  first, auto-dropping out after 7 days — items stay in their normal column either way), so a major
  incident doesn't get lost in the normal flow.
- A **Latest CVEs** section below that shows the 3 most recent items tagged `cve` (genuine vulnerability
  disclosures on major products, as opposed to generic `security` news) — no dedicated CVE feed, just a
  display pull from whatever the classifier already tagged that way.

**Two robustness details worth knowing about:**
- Item summaries have HTML tags stripped and entities decoded *before* truncating to length, not after —
  truncating raw HTML first can cut mid-tag and leave malformed markup that neither displays cleanly nor
  sends to the classifier cleanly (this was the actual cause of a full outage once: 1min.ai was returning
  an immediate `status: FAILURE` on every batch that included one of these malformed summaries, so
  nothing got classified for days).
- Raw feeds (especially Hacker News, which mirrors everything submitted, not just tech) occasionally
  include an item whose content trips 1min.ai's own moderation and gets the *entire batch* rejected with
  no per-item detail. If a batch fails, the app automatically bisects it and retries the halves, isolating
  which single item is the problem. A lone item is retried once more before being quarantined (marked
  classified, dropped, tagged `unclassifiable`) — this avoids permanently dropping a perfectly fine item
  that just happened to hit a transient API blip while alone in a bisected batch of one.

### Homelab Deals (`/deals`, `/deals/beyond`)

A separate pipeline from the RSS/news one, built around the eBay Browse API. Leave
`EBAY_CLIENT_ID`/`EBAY_CLIENT_SECRET` blank in `.env` to disable it entirely — nothing else breaks (get
free keys at [developer.ebay.com](https://developer.ebay.com) → create an app → production keys).

**Sourcing.** `app/ebay.py` polls eBay (searches defined in `app/deals.yaml` — ThinkCentre Tiny, EliteDesk
Mini, ProDesk Mini, NUC, OptiPlex Micro) across the marketplaces in `EBAY_MARKETPLACES` (default
`EBAY_FR,EBAY_DE,EBAY_GB` — Germany and the UK both have far more listings for this category than France
alone).

**Pricing.** Every listing's price + shipping is converted to EUR (`GBP_TO_EUR_RATE`, default 1.17, not
live-fetched — update it by hand occasionally) to get one true comparable total. That total decides which
page a listing lands on: `/deals` for anything up to `DEAL_MAX_PRICE_EUR` (default €500), `/deals/beyond`
for the rest up to `DEAL_EXTENDED_MAX_PRICE_EUR` (default €1500 — also the ceiling eBay's own search is
queried with, so "beyond budget" listings aren't excluded before we even see them). Both pages use the
same layout: three columns by price — **Cheapest / Mid-Range / Priciest**, each an even third, sorted
lowest-to-highest within itself, with the tier's actual price range shown in the column header. The price
badge on each card is always this EUR-equivalent total; the original native amount (and, for `EBAY_GB`
listings, a cross-border/import-VAT warning — post-Brexit charges eBay's API can't quote up front) stays
visible in the card's summary text for transparency.

**CPU keep/drop policy.** Before anything reaches the LLM, `ebay.py`'s `_too_old()` deterministically
rejects CPUs not worth listing for homelab use, plus an HP EliteDesk/ProDesk "G1"–"G3" chassis suffix (HP
encodes chassis generation right in the model name, predates DDR4 regardless of what CPU ended up in one).
The policy (`_intel_policy`) is mostly about hyperthreading/SMT — what actually matters for homelab
VM/container workloads — but with two deliberate departures from pure hyperthreading fact, not a blanket
"8th-gen-i7-or-newer" rule:

| Generation | i3 | i5 | i7 | i9 |
|---|---|---|---|---|
| 7th (Kaby Lake) | keep | drop (no HT) | keep | — |
| 8th (Coffee Lake) | drop | drop | keep | — |
| 9th (Coffee Lake Refresh) | drop | drop | **keep, flagged "no hyperthreading"** | keep |
| 10th–14th (Comet Lake–Raptor Lake) | **drop (policy)** | keep | keep | keep |
| Core Ultra Series 1 (Meteor Lake) | — | keep | keep | keep |
| Core Ultra Series 2 (Arrow/Lunar Lake) | — | drop | drop | drop |

The two bolded departures are deliberate: 9th-gen i7 lacks hyperthreading but is common/cheap enough to
list anyway with a caveat rather than exclude outright — the note ends up in the card's summary text, e.g.
"no hyperthreading (9th-gen i7)". i3 is excluded from 10th gen onward even though it does have hyperthreading
there — core counts stay low-end regardless, so it's excluded on policy, not on the HT fact. Everything else
in the table is the real hyperthreading answer for that generation+family. Core Ultra Series 2 is a hard
drop on every tier, no exceptions — even an "Ultra 9" lacks hyperthreading entirely. Pre-7th-gen Intel is
dropped outright regardless of tier. AMD Ryzen is simpler: pre-3000-series (1000/2000, Zen/Zen+) only
Ryzen 5/7 have SMT; 3000-series (Zen 2) onward virtually every model does, including most Ryzen 3. Matches
both numbered model text ("i5-8500") and worded generation text ("i5-7th Gen"), whichever the listing uses,
and runs against the title and, separately, whatever CPU text a chosen variation's own aspect data provides
— cheaper and more reliable than a prompt instruction to hope the LLM catches every time (the same table is
also embedded directly in the LLM prompt, as a second line of defense for text this regex-based check can't
parse). Two real failures drove building this: a 2013 ProDesk 600 G1 slipped through on generation alone,
and a listing titled "i7" whose only real selectable CPU turned out to be an i5-7th-gen (which, per the
table, wouldn't have qualified anyway) slipped through because the picker below was trusting the shared
title over the variation's own data. A listing with no CPU model or chassis generation stated anywhere just
passes through unaffected, for the LLM to judge on title/form-factor text as usual.

**Multi-variation listings.** "Choose your configuration" listings (one listing, several selectable
CPU/RAM/storage combos behind one or more dropdowns — common among bulk refurb sellers, and the reason
the search API's displayed price is often just the cheapest option) get one extra API call per unique
listing to fetch every variation's own price and aspects. `ebay.py` then deterministically picks the
configuration worth reporting — best CPU tier *among variations that state their own CPU and pass the
policy table above* (if the best any of them offers fails that table, the whole listing is rejected rather
than falling back to whatever the shared title claims), RAM as close to 32GB as possible (exact match
preferred), cheapest storage as the tiebreak since storage matters least — and uses *that* SKU's own real
price, not the cheapest-teaser one, with its own "no hyperthreading" flag if it's a 9th-gen i7. A listing
whose variations never state CPU at all (only RAM/storage vary; the title carries the sole CPU claim) still
falls back to letting the title judge it, same as a single-configuration listing — there's nothing more
specific to trust instead in that case. Aspect names/values vary a lot by seller and marketplace language
(English "Memory/RAM" vs French "RAM :" vs a single combined "Configuration" aspect), so the matching in
`_variation_specs`/`_pick_best_variation` is intentionally loose rather than expecting one exact format.
Some sellers run storage as a third, fully independent selector (separate CPU/RAM/Storage dropdowns) that
eBay's own variation-group data doesn't always expose per storage size — only CPU×RAM show up as priced
SKUs, with storage left as `null`/unselected in the API response even though the real listing page has a
size dropdown. Rather than silently reporting a price that may not actually include storage, the card's
summary says so explicitly ("price may not include storage - size wasn't resolved for this configuration,
check listing") whenever this happens, instead of the normal "price shown is for this exact configuration."

**Non-working listings.** `_not_working()` drops anything whose eBay `condition` field says "For parts or
not working" (eBay's own standardized condition string) — broken hardware isn't a deal regardless of specs
or price, so this is checked before anything else, alongside the age/CPU gate.

**Classification.** Results are stored the same way as feed items (`kind = 'deal'`) but classified with a
*different* system prompt (`classifier.py` `_DEALS_SYSTEM_PROMPT`) that judges the actual hardware match —
genuine mini/micro/tiny form factor, and the same CPU policy table as above (RAM upgradability isn't a hard
filter even when a listing doesn't state 32GB installed, since these platforms all support it by default).
The same prompt extracts CPU (with core/thread count filled in from the model's known specs), RAM, and
storage from the listing text, shown as spec badges on each card — eBay's search API doesn't return
structured item aspects for non-variation listings, so this relies on the seller having put specs in the
title, which is near-universal for this category.

**Availability re-checking.** A listing that gets kept stays in the DB indefinitely by default — nothing
re-validates it, so once a deal sells or gets taken down it would otherwise just sit there forever. Each
poll, `DEAL_RECHECK_BATCH_SIZE` (default 50) already-kept listings — the ones never checked or checked
longest ago — get looked up again via eBay's `get_item_by_legacy_id`; a 404 (listing ended) or an
`OUT_OF_STOCK` availability status gets it deleted outright. For a listing that was originally a
multi-variation "choose your configuration" one, that id turns out to belong to the *group*, not a single
item, and eBay's API rejects it with a 400 pointing at `get_items_by_item_group` instead — `ebay.py` falls
back to that endpoint automatically in that case, treating any remaining variation as still available.
This is a rolling sweep rather than re-checking everything every poll, so the per-poll cost stays flat no
matter how many listings have accumulated — with the default batch size, the whole set cycles through
roughly every couple of hours at a typical volume of some tens to ~150 kept listings.

### Refreshing

There's a "Refresh now" button on every page, safe to expose publicly: it's rate-limited to one manual
poll per `REFRESH_COOLDOWN_MINUTES` (default 10) so a shared link can't be used to spam paid LLM calls. It
greys out and reads "cooling down" while a recent refresh is still in effect. Clicking it returns instantly
(redirects straight back) — the actual fetch + classification runs in the background rather than blocking
the request, so it doesn't sit there long enough to trip a reverse-proxy timeout (this used to 502 after
~60s on a slow poll, then recover once the request finally finished server-side). A lock prevents two polls
(manual + the hourly schedule) from ever running at once. The page just won't show fresh results until the
background poll actually finishes — reload after a bit.

## Setup

1. `cp .env.example .env` and fill in `ONE_MIN_API_KEY` (the same key used by discord-1min-proxy). This
   alone is enough for the news feed.
2. Optionally, also fill in `EBAY_CLIENT_ID`/`EBAY_CLIENT_SECRET` to enable Homelab Deals (see below) —
   leave them blank to skip that part entirely, the news feed works either way.
3. `docker compose up -d --build`
4. Open `http://<host>:8085`

Upgrading an existing deployment is just `git pull && docker compose up -d --build` — the SQLite schema
migrates itself on startup, no manual steps or data loss.

## Tuning the filter

The keep/drop rules live in `app/classifier.py` (`_SYSTEM_PROMPT` for news, `_DEALS_SYSTEM_PROMPT` for
Homelab Deals). Edit either prompt directly if the filter is too strict or too loose, then rebuild
(`docker compose up -d --build`) — no code changes needed elsewhere.

## Adding/removing feeds

Edit `app/sources.yaml`. Any standard RSS/Atom feed URL works. Reddit subreddits: append `/.rss` to the
subreddit URL. No restart required — the file is volume-mounted and read fresh on each poll.

## Adding/removing eBay deal searches

Edit `app/deals.yaml` — each entry is just a `name` and an eBay `keywords` search string, run against every
marketplace in `EBAY_MARKETPLACES`. Volume-mounted like `sources.yaml`, so no rebuild needed.

## Re-qualifying stored deal listings after a pricing/parsing logic change

`insert_new_items()` only ever inserts *new* rows — it never touches ones already in the DB — so a change
to how `ebay.py` prices or parses listings (like the variation-picking logic) only affects new listings
going forward, not ones already stored. To make every existing deal get re-fetched and re-classified under
the current logic, wipe them and let the next poll repopulate from scratch:

```
docker compose exec feed-curator python reset_deals.py
```

Then either wait for the next hourly poll or trigger one immediately (see below).

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
