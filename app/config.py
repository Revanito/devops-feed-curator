from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    one_min_api_key: str
    one_min_base_url: str = "https://api.1min.ai"
    model_classifier: str = "gpt-4o-mini"

    poll_interval_minutes: int = 30
    classify_batch_size: int = 15
    max_items_per_source: int = 25

    sources_file: str = "sources.yaml"
    db_path: str = "/data/feeds.db"


settings = Settings()
