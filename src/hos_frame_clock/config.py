"""在模块首次导入时加载一次环境配置。"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    token: str | None = Field(default=None, validation_alias="PADDLEOCR_TOKEN", repr=False)


settings = Settings()
