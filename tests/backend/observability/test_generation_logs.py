"""Generation lifecycle logging covers failures before the provider is called."""

import asyncio

import pytest

from astrbot_plugin_image_studio.backend.gallery.store import GenerationStore
from astrbot_plugin_image_studio.backend.generation.service import (
    ImageGenerationService,
)
from astrbot_plugin_image_studio.backend.observability.store import StructuredLogStore
from astrbot_plugin_image_studio.tests.backend.config.test_parameter_policy import (
    CapturingExecutor,
    policy_provider,
    policy_settings,
)


def test_generation_logs_include_validation_failure_and_success(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        logs = StructuredLogStore(tmp_path)
        executor = CapturingExecutor()
        service = ImageGenerationService(
            settings=policy_settings(policy_provider()),
            executor=executor,
            store=store,
            logs=logs,
        )
        with pytest.raises(ValueError, match="提示词不能为空"):
            await service.generate(mode="text2img", provider_id="policy", prompt="")
        assert not executor.requests
        result = await service.generate(
            mode="text2img", provider_id="policy", prompt="a mountain"
        )
        entries = (await logs.query(category="generation"))["items"]
        assert len(entries) == 4
        error = next(item for item in entries if item["level"] == "ERROR")
        assert error["data"]["error_type"] == "ValueError"
        done = next(item for item in entries if item["message"] == "生图完成")
        assert done["data"]["generation_id"] == result.generation_id
        assert done["data"]["image_count"] == 1
        assert done["data"]["model_ref"] == "policy:paint"
        assert done["operation_id"] != error["operation_id"]
        assert (
            sum(item["operation_id"] == done["operation_id"] for item in entries) == 2
        )

    asyncio.run(run())


def test_log_storage_failure_does_not_fail_generation(tmp_path, monkeypatch):
    async def run():
        logs = StructuredLogStore(tmp_path)

        def fail(_entry):
            raise OSError("disk unavailable")

        monkeypatch.setattr(logs, "_append", fail)
        store = GenerationStore(tmp_path)
        await store.initialize()
        service = ImageGenerationService(
            settings=policy_settings(policy_provider()),
            executor=CapturingExecutor(),
            store=store,
            logs=logs,
        )
        result = await service.generate(
            mode="text2img", provider_id="policy", prompt="a mountain"
        )
        assert len(result.images) == 1

    asyncio.run(run())
