# devops-feed-curator

A small self-hosted webpage that pulls IT/DevOps/homelab/IT-news RSS feeds (Reddit, Hacker News, and a
handful of blogs and security/news sites) and uses an LLM to filter out the noise — deal posts, "what
should I buy" threads, off-topic gadget content — keeping only substantive self-hosting/DevOps/homelab
items and admin-relevant IT world news.

## How it works

- `app/sources.yaml` lists the feeds to poll (reddit subs, HN, blogs, IT-news sites like BleepingComputer,
  Krebs on Security, SANS ISC, and the Microsoft Security Blog). It's volume-mounted into the container,
  so edits take effect on the next poll cycle without a rebuild.
- Every `POLL_INTERVAL_MINUTES` (default 60), the app fetches all feeds, stores new items in a local
  SQLite DB (`data/feeds.db`), then sends unclassified items in batches to a cheap LLM (`gpt-4o-mini` via
  1min.ai, same API key/provider as [discord-1min-proxy](../discord-1min-proxy)) asking it to keep/drop
  each one, tag it with a topic, and flag it `critical` if it's a big, broadly-impactful incident (major
  outages, actively-exploited zero-days, CrowdStrike-style events) rather than routine news.
- The webpage at `/` shows the kept items in three columns — Reddit, Blogs & DevOps News, and Homelab
  (homelab-tagged items win that column regardless of source) — newest first within each. Admin-relevant
  IT world news lands in Blogs & DevOps News, tagged accordingly (e.g. `microsoft`, `outage`, `security`).
- Anything flagged `critical` also shows in a **Must Read** banner above the three columns (capped at 8,
  newest first), so a major incident doesn't get lost in the normal flow. Items older than 7 days drop
  out of Must Read automatically (they stay in their normal column, just not the banner).
- There's a "Refresh now" button, safe to expose publicly: it's rate-limited to one manual poll per
  `REFRESH_COOLDOWN_MINUTES` (default 10) so a shared link can't be used to spam paid LLM calls. It greys
  out and reads "cooling down" while a recent refresh is still in effect.

## Setup

1. `cp .env.example .env` and fill in `ONE_MIN_API_KEY` (the same key used by discord-1min-proxy).
2. `docker compose up -d --build`
3. Open `http://<host>:8085`

Upgrading an existing deployment (e.g. after pulling the `critical`/Must Read change) is just
`git pull && docker compose up -d --build` — the SQLite schema migrates itself on startup, no manual
steps or data loss.

## Tuning the filter

The keep/drop rules live in `app/classifier.py` (`_SYSTEM_PROMPT`). Edit that prompt directly if the
filter is too strict or too loose, then rebuild (`docker compose up -d --build`) — no code changes
needed elsewhere.

## Adding/removing feeds

Edit `app/sources.yaml`. Any standard RSS/Atom feed URL works. Reddit subreddits: append `/.rss` to the
subreddit URL. No restart required — the file is volume-mounted and read fresh on each poll.

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
