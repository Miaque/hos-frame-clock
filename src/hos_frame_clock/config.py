"""在模块首次导入时加载一次环境配置。"""

from collections.abc import Iterator
from functools import cached_property
from itertools import cycle

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    tokens: list[str] = Field(
        default_factory=list, validation_alias="PADDLEOCR_TOKENS", repr=False
    )

    @cached_property
    def _rotation(self) -> Iterator[str]:
        return cycle(self.tokens)

    def next_token(self) -> str | None:
        """按配置顺序轮询返回下一个 Token；未配置任何 Token 时返回 None。"""
        return next(self._rotation, None)


settings = Settings()
