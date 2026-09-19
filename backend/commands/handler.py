"""Chat-command orchestration using explicitly supplied host entrypoints."""

from typing import Any
from astrbot.api.message_components import Image, Plain
from ..providers.executor import ProviderError
from .parser import COMMAND_HELP, parse_command


async def run_image_command(
    event,
    *,
    get_service,
    get_settings,
    resolve_sources,
    resolve_references,
    invocation_source,
):
    """Yield the existing command results without owning host event state."""
    try:
        options = parse_command(str(event.message_str or ""), allow_empty_prompt=True)
        if options["help"]:
            yield event.plain_result(COMMAND_HELP)
            return
        service = get_service()
        mode = options["mode"]
        explicit_text = options["mode_explicit"] and mode.strip().lower() in {
            "text2img",
            "text",
            "txt2img",
        }
        sources = (
            []
            if explicit_text
            else await resolve_sources(event, options["reference_paths"])
        )
        if not options["mode_explicit"] and sources:
            mode = "img2img"
        selected_provider, selected_model = service.resolve_command_model(
            mode=mode, provider_id=options["provider_id"], model=options["model"]
        )
        if not options["prompt"] and (
            selected_provider.kind != "comfyui"
            or selected_model.comfyui_capabilities["prompt_required"]
        ):
            raise ValueError("请填写提示词；使用 /istudio --help 查看指令帮助")
        references = (
            await resolve_references(
                event,
                ordered_sources=sources,
                max_images=selected_model.max_reference_images,
                reject_excess=selected_provider.kind in {"novelai_official", "comfyui"},
            )
            if sources
            else ()
        )
        if (
            not explicit_text
            and (options["mode_explicit"] or sources)
            and not references
        ):
            raise ValueError(
                "图生图未读取到可用参考图，请附带图片、引用含图消息或填写 --ref"
            )
        result = await service.generate(
            mode=mode,
            provider_id=options["provider_id"],
            prompt=options["prompt"],
            negative_prompt=options["negative_prompt"],
            model=options["model"],
            size=options["size"],
            count=options["count"],
            parameters=options["parameters"],
            references=references,
            source="command",
            invocation_source=(
                invocation_source(event)
                if get_settings().history.record_invocation_identity
                else None
            ),
        )
    except (ValueError, ProviderError) as exc:
        yield event.plain_result(f"生图失败：{exc}")
        return
    message = f"已生成 {len(result.images)} 张图片"
    if result.warning:
        message += f"\n{result.warning}"
    chain: list[Any] = [Plain(message)]
    chain.extend(Image.fromBytes(image.data) for image in result.images)
    yield event.chain_result(chain)
