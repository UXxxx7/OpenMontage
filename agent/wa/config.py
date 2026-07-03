import os


class Settings:
    """Lazy-read settings from environment variables."""

    @property
    def whatsapp_phone_number_id(self) -> str:
        return os.environ.get("WA_PHONE_ID", "")

    @property
    def whatsapp_access_token(self) -> str:
        return os.environ.get("WA_TOKEN", "")

    @property
    def whatsapp_api_version(self) -> str:
        return os.environ.get("WA_API_VERSION", "v25.0")

    @property
    def whatsapp_verify_token(self) -> str:
        return os.environ.get("WA_VERIFY_TOKEN", "")

    @property
    def whatsapp_app_secret(self) -> str:
        return os.environ.get("WA_APP_SECRET", "")


settings = Settings()
