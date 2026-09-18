"""在模块首次导入时加载一次环境配置。"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import cached_property

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
    concurrency_per_token: int = Field(
        default=1, ge=1, validation_alias="PADDLEOCR_CONCURRENCY_PER_TOKEN"
    )

    @cached_property
    def _pool(self) -> asyncio.Queue[str]:
        pool: asyncio.Queue[str] = asyncio.Queue()
        for token in self.tokens * self.concurrency_per_token:
            pool.put_nowait(token)
        return pool

    @asynccontextmanager
    async def lease_token(self) -> AsyncIterator[str]:
        """借出一个空闲 Token，全部占用时等待；退出时归还。"""
        if not self.tokens:
            raise ValueError("请在导入前通过 .env 或环境变量设置 PADDLEOCR_TOKENS")
        token = await self._pool.get()
        try:
            if not token.strip():
                raise ValueError("PADDLEOCR_TOKENS 中存在空 Token")
            yield token
        finally:
            self._pool.put_nowait(token)


settings = Settings()
