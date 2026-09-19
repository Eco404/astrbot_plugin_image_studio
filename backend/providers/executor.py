"""Public provider dispatcher; protocol implementations live in their adapters."""

from __future__ import annotations

from typing import Any

import aiohttp

from ..models import GeneratedImage, GenerationRequest, ImageProvider
from . import custom_json, discovery, gemini, nai_direct, openai_images
from .errors import ProviderBatchError, ProviderError, ProviderPartialResponseError
from .http import ImageResponseReader
from .novelai.client import NovelAIClient

__all__ = [
    "ProviderBatchError",
    "ProviderError",
    "ProviderExecutor",
    "ProviderPartialResponseError",
]


class ProviderExecutor:
    """Dispatch requests while retaining shared sessions and stateful clients."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self._images = ImageResponseReader(session)
        self._novelai = NovelAIClient(session)
        self._comfyui = None

    async def fetch_quota(self, provider: ImageProvider) -> dict[str, Any]:
        if provider.kind == "novelai_official":
            return await self._novelai.fetch_quota(provider)
        return await nai_direct.fetch_quota(self.session, provider)

    async def discover_models(self, provider: ImageProvider) -> list[dict[str, Any]]:
        return await discovery.discover_models(self.session, provider)

    async def generate(
        self,
        provider: ImageProvider,
        request: GenerationRequest,
    ) -> tuple[GeneratedImage, ...]:
        """Run one provider request and normalize its returned images."""

        if provider.kind == "openai_images":
            return await openai_images.generate(
                self.session, self._images, provider, request
            )
        if provider.kind == "gemini":
            return await gemini.generate(self.session, self._images, provider, request)
        if provider.kind == "nai_direct":
            return await nai_direct.generate(
                self.session, self._images, provider, request
            )
        if provider.kind == "novelai_official":
            return await self._novelai.generate(provider, request)
        if provider.kind == "custom_json":
            return await custom_json.generate(
                self.session, self._images, provider, request
            )
        if provider.kind == "comfyui":
            from .comfyui.client import ComfyClient, ComfyExecutionError

            if self._comfyui is None:
                self._comfyui = ComfyClient(self.session)
            try:
                return await self._comfyui.execute(provider, request)
            except ComfyExecutionError as exc:
                if exc.images:
                    raise ProviderPartialResponseError(
                        exc.images, exc.failures or ((1, str(exc)),)
                    ) from exc
                raise ProviderError(str(exc)) from exc
        raise ProviderError(f"不支持的 Provider 类型: {provider.kind}")
