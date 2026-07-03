import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    discord_token: str
    xai_api_key: str
    glot_api_token: str
    glot_base_url: str
    glot_api_mode: str
    api_ninjas_key: str
    api_football_key: str
    api_football_base_url: str
    xai_model: str
    xai_vision_model: str
    xai_image_model: str
    db_path: str
    default_prefix: str
    bot_owner_ids: tuple[int, ...]


def load_settings() -> Settings:
    load_dotenv()

    discord_token = os.getenv("DISCORD_TOKEN", "").strip()
    xai_api_key = os.getenv("XAI_API_KEY", "").strip()
    glot_api_token = os.getenv("GLOT_API_TOKEN", "").strip()
    glot_base_url = os.getenv("GLOT_BASE_URL", "https://run.glot.io").strip() or "https://run.glot.io"
    glot_api_mode = os.getenv("GLOT_API_MODE", "run_api").strip().lower() or "run_api"
    api_ninjas_key = os.getenv("API_NINJAS_KEY", "").strip()
    api_football_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    api_football_base_url = os.getenv(
        "API_FOOTBALL_BASE_URL",
        "https://v3.football.api-sports.io",
    ).strip() or "https://v3.football.api-sports.io"
    xai_model = os.getenv("XAI_MODEL", "grok-4-1-fast-reasoning").strip()
    xai_vision_model = os.getenv("XAI_VISION_MODEL", xai_model).strip() or xai_model
    xai_image_model = os.getenv("XAI_IMAGE_MODEL", "grok-imagine-image-quality").strip()
    xai_image_model = xai_image_model or "grok-imagine-image-quality"
    db_path = os.getenv("DB_PATH", "data/bot.db").strip()
    default_prefix = os.getenv("DEFAULT_PREFIX", "!").strip() or "!"
    raw_owner_ids = os.getenv("BOT_OWNER_IDS", "").strip()
    owner_ids: list[int] = []
    if raw_owner_ids:
        for token in raw_owner_ids.split(","):
            value = token.strip()
            if not value:
                continue
            if value.isdigit():
                owner_ids.append(int(value))
    if not discord_token:
        raise ValueError("Missing DISCORD_TOKEN in environment")
    if not xai_api_key:
        raise ValueError("Missing XAI_API_KEY in environment")

    return Settings(
        discord_token=discord_token,
        xai_api_key=xai_api_key,
        glot_api_token=glot_api_token,
        glot_base_url=glot_base_url,
        glot_api_mode=glot_api_mode,
        api_ninjas_key=api_ninjas_key,
        api_football_key=api_football_key,
        api_football_base_url=api_football_base_url,
        xai_model=xai_model,
        xai_vision_model=xai_vision_model,
        xai_image_model=xai_image_model,
        db_path=db_path,
        default_prefix=default_prefix,
        bot_owner_ids=tuple(owner_ids),
    )
