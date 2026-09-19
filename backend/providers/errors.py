"""Provider failures shared by adapters and the generation coordinator."""

from __future__ import annotations

from ..models import GeneratedImage


class ProviderError(RuntimeError):
    """A user-presentable upstream provider failure."""


class ProviderPartialResponseError(ProviderError):
    """Preserve valid images when another result in one response is invalid."""

    def __init__(
        self,
        images: tuple[GeneratedImage, ...],
        failures: tuple[tuple[int, str], ...],
    ) -> None:
        self.images = images
        self.failures = failures
        details = "；".join(f"第 {index} 项：{reason}" for index, reason in failures)
        super().__init__(
            f"生图响应中有 {len(failures)} 项无效，已保留 {len(images)} 张图片。"
            f"{details}。请求可能已消耗额度，请勿自动重试。"
        )


class ProviderBatchError(ProviderError):
    """Carry all successful images through a partially failed split batch."""

    def __init__(
        self,
        requested: int,
        images: tuple[GeneratedImage, ...],
        failures: tuple[tuple[int, str], ...],
        *,
        request_sizes: tuple[int, ...] = (),
        actual_response_counts: tuple[int, ...] = (),
    ) -> None:
        self.images = images
        self.failures = failures
        sizes = request_sizes or (1,) * requested
        failed_images = sum(
            max(
                0,
                sizes[index - 1]
                - (actual_response_counts[index - 1] if actual_response_counts else 0),
            )
            for index, _ in failures
        )
        details = "；".join(
            f"第 {index} 次请求（计划 {sizes[index - 1]} 张）：{reason}"
            for index, reason in failures
        )
        failure_label = (
            "失败或部分失败"
            if actual_response_counts
            and any(actual_response_counts[index - 1] for index, _ in failures)
            else "失败"
        )
        summary = (
            f"本批目标 {requested} 张，发送 {len(sizes)} 次请求，"
            f"成功 {len(sizes) - len(failures)} 次，{failure_label} {len(failures)} 次"
            f"（涉及目标图片 {failed_images} 张）；实际返回 {len(images)} 张，已全部保留。"
        )
        super().__init__(
            summary + f"{details}。失败请求可能已消耗额度，请勿自动重试整个批次。"
        )
