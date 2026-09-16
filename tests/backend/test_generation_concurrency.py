"""Admission ownership across live services, snapshots and account aliases."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from astrbot_plugin_image_studio.backend.generation.concurrency import (
    GenerationConcurrency,
)
from astrbot_plugin_image_studio.backend.generation.service import (
    ImageGenerationService,
)
from astrbot_plugin_image_studio.tests.backend.test_model_batches import (
    BatchExecutor,
    provider,
)
from astrbot_plugin_image_studio.tests.backend.test_nai_batches import generate, service


def test_borrowed_execution_snapshot_cannot_undo_live_limit_reduction():
    async def run():
        original = provider(native_batch_size=1, max_concurrent_requests=2, limit=2)
        executor = BatchExecutor(blocked=True)
        owner = service([original], executor=executor)
        original_settings = owner.settings
        first = asyncio.create_task(generate(owner, count=2))
        await executor.wait_started(2)
        current = replace(
            original,
            max_concurrent_generations=1,
            models=(replace(original.models[0], max_concurrent_requests=1),),
        )
        owner.update_settings(replace(owner.settings, providers=(current,)))
        execution = ImageGenerationService(
            settings=original_settings,
            executor=executor,
            store=owner.store,
            concurrency=owner.concurrency,
        )
        assert execution.concurrency is owner.concurrency
        execution.update_settings(original_settings)
        second = asyncio.create_task(generate(execution, count=2))
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert len(executor.calls) == 2
            executor.gates[0].set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert len(executor.calls) == 2, "snapshot must not restore old capacity"
        finally:
            executor.release_all()
            await asyncio.wait_for(asyncio.gather(first, second), 2)
        assert len(executor.calls) == 4
        assert executor.model_active[("studio", "paint")] == 0

    asyncio.run(run())


@pytest.mark.parametrize("limit_name", ["provider", "model"])
def test_live_limit_increase_wakes_borrowed_queued_requests(limit_name):
    async def run():
        original = provider(
            native_batch_size=1,
            max_concurrent_requests=1 if limit_name == "model" else 3,
            limit=1 if limit_name == "provider" else 3,
        )
        executor = BatchExecutor(blocked=True)
        owner = service([original], executor=executor)
        execution = ImageGenerationService(
            settings=owner.settings,
            executor=executor,
            store=owner.store,
            concurrency=owner.concurrency,
        )
        tasks = [asyncio.create_task(generate(execution, count=1)) for _ in range(3)]
        await executor.wait_started(1)
        await asyncio.sleep(0)
        assert len(executor.calls) == 1
        updated = replace(
            original,
            max_concurrent_generations=3,
            models=(replace(original.models[0], max_concurrent_requests=3),),
        )
        try:
            owner.update_settings(replace(owner.settings, providers=(updated,)))
            await executor.wait_started(2)
            assert len(executor.calls) == 3
        finally:
            executor.release_all()
            await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert executor.peak["studio"] == 3

    asyncio.run(run())


def test_cancelled_account_waiter_releases_model_and_provider_slots():
    async def run():
        manager = GenerationConcurrency()
        first = replace(provider(), kind="novelai_official", api_key="same-token")
        second = replace(first, id="alias", max_concurrent_generations=1)
        other_account = replace(second, api_key="other-token")
        model = replace(first.models[0], max_concurrent_requests=1)
        owner_entered = asyncio.Event()
        release_owner = asyncio.Event()

        async def hold_owner():
            async with manager.slot(first, model):
                owner_entered.set()
                await release_owner.wait()

        async def waiting_alias():
            async with manager.slot(second, model):
                pytest.fail("same account must not execute in parallel")

        holding = asyncio.create_task(hold_owner())
        await owner_entered.wait()
        waiter = asyncio.create_task(waiting_alias())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        async def enter_other_account():
            async with manager.slot(other_account, model):
                return "entered"

        try:
            assert await asyncio.wait_for(enter_other_account(), 1) == "entered"
        finally:
            release_owner.set()
            await holding
        async with manager.slot(second, model):
            pass

    asyncio.run(run())


def test_failure_releases_all_admission_levels_for_next_request():
    async def run():
        manager = GenerationConcurrency()
        configured = replace(
            provider(limit=1), kind="novelai_official", api_key="token"
        )
        model = replace(configured.models[0], max_concurrent_requests=1)
        with pytest.raises(RuntimeError, match="provider failed"):
            async with manager.slot(configured, model):
                raise RuntimeError("provider failed")

        async def retry():
            async with manager.slot(configured, model):
                return True

        assert await asyncio.wait_for(retry(), 1)

    asyncio.run(run())
