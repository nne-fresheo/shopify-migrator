from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Required env var {name!r} is not set. Check your .env file.")
    return val


def _optional(name: str, default: str) -> str:
    return os.getenv(name, default)


@dataclass(frozen=True)
class Config:
    source_shop: str
    source_token: str
    source_client_id: str
    source_client_secret: str
    dest_shop: str
    dest_token: str
    dest_client_id: str
    dest_client_secret: str
    api_version: str
    data_dir: Path
    log_dir: Path
    log_level: str
    rest_bucket_size: int
    rest_refill_rate: float
    graphql_max_cost: int
    graphql_restore_rate: float
    graphql_cost_threshold: int
    max_retries: int
    django_database_url: str
    description_template: Path
    django_media_url: str
    meal_locale: str
    subscription_group_code: str

    @property
    def id_maps_dir(self) -> Path:
        return self.data_dir / "id_maps"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def progress_file(self) -> Path:
        return self.log_dir / "progress.json"

    @property
    def failed_resources_file(self) -> Path:
        return self.log_dir / "failed_resources.json"

    @property
    def migration_log_file(self) -> Path:
        return self.log_dir / "migration.log"


def load_config() -> Config:
    data_dir = Path(_optional("DATA_DIR", "./data")).resolve()
    log_dir = Path(_optional("LOG_DIR", "./logs")).resolve()

    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "id_maps").mkdir(parents=True, exist_ok=True)
    (data_dir / "tmp").mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        source_shop=_require("SOURCE_SHOP_DOMAIN"),
        source_token=_optional("SOURCE_ACCESS_TOKEN", ""),
        source_client_id=_optional("SOURCE_CLIENT_ID", ""),
        source_client_secret=_optional("SOURCE_CLIENT_SECRET", ""),
        dest_shop=_require("DEST_SHOP_DOMAIN"),
        dest_token=_optional("DEST_ACCESS_TOKEN", ""),
        dest_client_id=_optional("DEST_CLIENT_ID", ""),
        dest_client_secret=_optional("DEST_CLIENT_SECRET", ""),
        api_version=_optional("API_VERSION", "2024-01"),
        data_dir=data_dir,
        log_dir=log_dir,
        log_level=_optional("LOG_LEVEL", "INFO"),
        rest_bucket_size=int(_optional("REST_BUCKET_SIZE", "40")),
        rest_refill_rate=float(_optional("REST_REFILL_RATE", "2")),
        graphql_max_cost=int(_optional("GRAPHQL_MAX_COST", "1000")),
        graphql_restore_rate=float(_optional("GRAPHQL_RESTORE_RATE", "50")),
        graphql_cost_threshold=int(_optional("GRAPHQL_COST_THRESHOLD", "200")),
        max_retries=int(_optional("MAX_RETRIES", "5")),
        django_database_url=_optional("DJANGO_DATABASE_URL", ""),
        description_template=Path(
            _optional("DESCRIPTION_TEMPLATE", "./templates/product_description.html.j2")
        ).resolve(),
        django_media_url=_optional("DJANGO_MEDIA_URL", ""),
        meal_locale=_optional("MEAL_LOCALE", "fr"),
        subscription_group_code=_optional("SHOPIFY_SUBSCRIPTION_GROUP_CODE", "main-bundle"),
    )
