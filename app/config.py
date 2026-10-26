from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    one_min_api_key: str
    one_min_base_url: str = "https://api.1min.ai"
    model_classifier: str = "gpt-4o-mini"
    # Used when the primary is down (retired model ID, provider outage). Deliberately a different
    # provider (AWS Bedrock vs OpenAI) so one provider's outage doesn't take out both. Blank = no fallback.
    model_classifier_fallback: str = "us.anthropic.claude-haiku-5-5"

    # Automatic poll runs once a day at this hour:minute (server local time) instead of on a fixed
    # interval, to keep classification token usage down. Use the "Refresh now" button (or POST
    # /refresh with an admin token) to poll on demand between scheduled runs.
    poll_hour: int = 0
    poll_minute: int = 0
    refresh_cooldown_minutes: int = 10
    classify_batch_size: int = 15
    max_items_per_source: int = 25

    # News/security feeds, CISA KEV and NVD are polled on this interval (deals stay on the daily
    # POLL_HOUR schedule). Only new items reach the LLM, so polling more often costs little extra.
    news_poll_minutes: int = 60

    sources_file: str = "sources.yaml"
    # Products you run/support - CVEs from NVD and CISA KEV are matched against this list.
    stack_file: str = "stack.yaml"
    kev_url: str = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
    nvd_url: str = "https://services.nvd.nist.gov/rest/json/cves/2.0"
    # Optional, free (nvd.nist.gov/developers/request-an-api-key): raises NVD's rate limit so
    # multi-page fetches are faster. Works fine without it.
    nvd_api_key: str = ""
    # NVD asks API users not to poll for updates more often than every two hours.
    nvd_poll_minutes: int = 120
    # CVEs published longer ago than this are ignored even if NVD re-scores them now, and
    # vulnerability rows are kept this long (so an old one can't reappear as "new").
    advisory_max_age_days: int = 30
    db_path: str = "/data/feeds.db"

    # The Security page shows what was flagged in the last this-many days; news items older than
    # this get deleted each poll.
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

    # Fallback GBP->EUR rate, only used if the daily live rate fetch (ebay.refresh_gbp_rate) fails.
    gbp_to_eur_rate: float = 1.17

    # How many already-kept deal listings to re-check against eBay per poll, to catch ones that
    # have sold/ended since we last saw them. A rolling sweep rather than checking everything
    # every time, so the per-poll cost stays flat regardless of how many listings have piled up.
    deal_recheck_batch_size: int = 50


settings = Settings()
