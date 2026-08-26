from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    one_min_api_key: str
    one_min_base_url: str = "https://api.1min.ai"
    model_classifier: str = "gpt-4o-mini"

    # Automatic poll runs once a day at this hour:minute (server local time) instead of on a fixed
    # interval, to keep classification token usage down. Use the "Refresh now" button (or POST
    # /refresh with an admin token) to poll on demand between scheduled runs.
    poll_hour: int = 0
    poll_minute: int = 0
    refresh_cooldown_minutes: int = 10
    classify_batch_size: int = 15
    max_items_per_source: int = 25

    sources_file: str = "sources.yaml"
    db_path: str = "/data/feeds.db"

    # News items (Must Read, Latest CVEs, and the 3 columns) older than this many days get deleted
    # each poll, rather than lingering indefinitely when a quiet stretch doesn't produce enough fresh
    # volume to push them off the page.
    news_max_age_days: int = 7

    # Reddit's anonymous RSS access blocks any request that follows another too soon (a single
    # combined multireddit request works, a second one seconds later doesn't) - so it's fetched on
    # its own clock, independent of the news/deals poll and manual refreshes, at most once every
    # this many hours. Tune this up if 429s/blocks come back, down if it turns out to be too
    # conservative - no code change needed either way.
    reddit_poll_hours: int = 8

    # Optional: set to enable POST /refresh (send as X-Admin-Token header). Leave blank to disable it entirely.
    admin_token: str = ""

    # eBay Browse API (developer.ebay.com -> create app -> production keys). Leave blank to disable
    # the Homelab Deals column entirely - it just stays empty rather than erroring.
    ebay_client_id: str = ""
    ebay_client_secret: str = ""
    ebay_marketplaces: str = "EBAY_FR,EBAY_DE,EBAY_GB"
    # ISO country code eBay's search filter uses to only return listings that can actually be
    # delivered here - no point surfacing a UK listing with no shipping to France.
    ebay_delivery_country: str = "FR"
    deal_max_price_eur: int = 500
    # Ceiling for the separate "beyond budget" page - keeps wildly unrelated high-end gear out
    # while still surfacing higher-end matches for a looser budget.
    deal_extended_max_price_eur: int = 1500
    deals_file: str = "deals.yaml"
    ram_deals_file: str = "ram_deals.yaml"
    # RAM has its own, much lower price ceilings than mini-PC deals - a stick/kit rarely costs
    # anywhere near what a whole mini PC does.
    ram_max_price_eur: int = 200
    ram_extended_max_price_eur: int = 500

    # Baseline GBP->EUR rate, not live-fetched - update this occasionally by hand if it drifts far
    # from reality. Good enough for "is this roughly under budget", not for actual accounting.
    gbp_to_eur_rate: float = 1.17

    # How many already-kept deal listings to re-check against eBay per poll, to catch ones that
    # have sold/ended since we last saw them. A rolling sweep rather than checking everything
    # every time, so the per-poll cost stays flat regardless of how many listings have piled up.
    deal_recheck_batch_size: int = 50


settings = Settings()
