# devops-feed-curator

A small self-hosted webpage that pulls IT/DevOps/homelab RSS feeds (Reddit, Hacker News, and a few
blogs) and uses an LLM to filter out the noise — deal posts, "what should I buy" threads, off-topic
gadget content — keeping only substantive self-hosting/DevOps/homelab items.

## How it works

- `app/sources.yaml` lists the feeds to poll (reddit subs, HN, blogs). It's volume-mounted into the
  container, so edits take effect on the next poll cycle without a rebuild.
- Every `POLL_INTERVAL_MINUTES` (default 30), the app fetches all feeds, stores new items in a local
  SQLite DB (`data/feeds.db`), then sends unclassified items in batches to a cheap LLM (`gpt-4o-mini` via
  1min.ai, same API key/provider as [discord-1min-proxy](../discord-1min-proxy)) asking it to keep/drop
  each one and tag it with a topic.
- The webpage at `/` shows only the kept items, newest first. There's no manual refresh button on the
  page (it's meant to be shared publicly, and a public button would let anyone trigger paid LLM calls) —
  the scheduled poll is the only trigger unless `ADMIN_TOKEN` is set, see below.

## Setup

1. `cp .env.example .env` and fill in `ONE_MIN_API_KEY` (the same key used by discord-1min-proxy).
2. `docker compose up -d --build`
3. Open `http://<host>:8085`

## Tuning the filter

The keep/drop rules live in `app/classifier.py` (`_SYSTEM_PROMPT`). Edit that prompt directly if the
filter is too strict or too loose, then rebuild (`docker compose up -d --build`) — no code changes
needed elsewhere.

## Adding/removing feeds

Edit `app/sources.yaml`. Any standard RSS/Atom feed URL works. Reddit subreddits: append `/.rss` to the
subreddit URL. No restart required — the file is volume-mounted and read fresh on each poll.

## Forcing a refresh manually

Set `ADMIN_TOKEN` in `.env` to any random string, then:

```
curl -X POST -H "X-Admin-Token: <your token>" https://feeds.vaultinc.fr/refresh
```

Leave `ADMIN_TOKEN` blank (the default) to disable `/refresh` entirely — it 404s.

## Exposing it publicly

Deployed at [feeds.vaultinc.fr](https://feeds.vaultinc.fr), reverse-proxied the same way as
[site.vaultinc.fr](https://site.vaultinc.fr) — public HTTPS traffic hits an existing LXC reverse-proxy
nginx that terminates TLS and `proxy_pass`es to this container's port 8085 on its own LXC. The read-only
page itself needs no auth (it's just links out to public articles); only `/refresh` is gated, see above.
