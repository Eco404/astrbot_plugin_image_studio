"""Shared generation slots for live services and durable execution views."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from ..config import RuntimeSettings
from ..models import ImageModel, ImageProvider


class _ResizableLimiter:
    """Resize future admissions while preserving all currently occupied slots."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.active = 0
        self.changed = asyncio.Event()

    def resize(self, limit: int) -> None:
        self.limit = limit
        self.changed.set()

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        while self.active >= self.limit:
            self.changed.clear()
            await self.changed.wait()
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1
            self.changed.set()


class GenerationConcurrency:
    """One shared admission controller per plugin runtime.

    Only the settings owner calls ``update_settings``. Execution views use
    ``slot`` with their captured provider/model data; this initializes missing
    temporary-model slots without replacing the owner's current limits. Entries
    are retained across settings updates because running and queued jobs may
    still hold them, including jobs for temporarily removed models.
    """

    def __init__(self) -> None:
        self._providers: dict[str, _ResizableLimiter] = {}
        self._models: dict[tuple[str, str], _ResizableLimiter] = {}
        self._accounts: dict[str, _ResizableLimiter] = {}

    def update_settings(self, settings: RuntimeSettings) -> None:
        for provider in settings.providers:
            self._provider(provider).resize(provider.max_concurrent_generations)
            for model in provider.models:
                self._model(provider, model).resize(model.max_concurrent_requests)

    def _provider(self, provider: ImageProvider) -> _ResizableLimiter:
        return self._providers.setdefault(
            provider.id, _ResizableLimiter(provider.max_concurrent_generations)
        )

    def _model(self, provider: ImageProvider, model: ImageModel) -> _ResizableLimiter:
        return self._models.setdefault(
            (provider.id, model.id), _ResizableLimiter(model.max_concurrent_requests)
        )

    @asynccontextmanager
    async def slot(
        self, provider: ImageProvider, model: ImageModel
    ) -> AsyncIterator[None]:
        """Acquire model, provider, then optional official-account slots.

        A busy model waits before occupying provider capacity. Official NovelAI
        aliases sharing the same credential additionally share one account slot.
        All acquired slots are released on completion, error or cancellation.
        """

        model_limiter = self._model(provider, model)
        provider_limiter = self._provider(provider)
        account_limiter = None
        if provider.kind == "novelai_official":
            account_key = hashlib.sha256(provider.api_key.strip().encode()).hexdigest()
            account_limiter = self._accounts.setdefault(
                account_key, _ResizableLimiter(1)
            )
        async with model_limiter.slot():
            async with provider_limiter.slot():
                if account_limiter is None:
                    yield
                else:
                    async with account_limiter.slot():
                        yield
