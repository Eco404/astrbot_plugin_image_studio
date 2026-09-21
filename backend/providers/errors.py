"""Provider failures shared by adapters and the generation coordinator."""

from __future__ import annotations

from typing import Literal

from ..models import GeneratedImage


class ProviderError(RuntimeError):
    """A user-presentable upstream provider failure."""

    def __init__(
        self,
        message: str,
        *,
        request_status: Literal["not_submitted", "rejected", "unknown"] = "unknown",
    ) -> None:
        super().__init__(message)
        self.request_status = request_status


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
        failure_states: tuple[tuple[int, str], ...] = (),
    ) -> None:
        self.images = images
        self.failures = failures
        self.failure_states = failure_states
        states_by_index = dict(failure_states)
        states = {states_by_index.get(index, "unknown") for index, _ in failures}
        request_status = (
            "not_submitted"
            if not images and states == {"not_submitted"}
            else "rejected"
            if not images
            and "rejected" in states
            and states <= {"not_submitted", "rejected"}
            else "unknown"
        )
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
        preflight_failures = sum(
            states_by_index.get(index) == "not_submitted" for index, _ in failures
        )
        summary = (
            f"本批目标 {requested} 张，计划 {len(sizes)} 次请求，"
            f"成功 {len(sizes) - len(failures)} 次，{failure_label} {len(failures)} 次"
            f"（涉及目标图片 {failed_images} 张）；实际返回 {len(images)} 张，已全部保留。"
        )
        if preflight_failures:
            summary += f"其中 {preflight_failures} 次在提交前失败。"
        guidance = (
            "本批没有提交生图请求；修正输入后再提交。"
            if request_status == "not_submitted"
            else "失败请求可能已消耗额度，请勿自动重试整个批次。"
        )
        super().__init__(
            summary + f"{details}。{guidance}",
            request_status=request_status,
        )
