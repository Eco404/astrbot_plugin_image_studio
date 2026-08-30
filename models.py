"""Value objects shared by Image Studio services."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

GenerationMode = Literal["text2img", "img2img"]


COMMON_MODEL_PARAMETERS: dict[str, dict[str, Any]] = {
    "size": {
        "type": "text",
        "label": "尺寸",
        "default": "1024x1024",
        "request_key": "size",
    },
    "count": {
        "type": "number",
        "label": "数量",
        "default": 1,
        "min": 1,
        "max": 4,
        "request_key": "count",
    },
}

NAI_DEFAULT_NEGATIVE = (
    "{{bad anatomy}},{bad feet},bad hands,{{{bad proportions}}},{blurry},cloned face,cropped,"
    "{{{deformed}}},{{{disfigured}}},error,{{{extra arms}}},{extra digit},{{{extra legs}}},extra limbs,"
    "{{extra limbs}},{fewer digits},{{{fused fingers}}},gross proportions,ink eyes,ink hair,"
    "jpeg artifacts,{{{{long neck}}}},low quality,{malformed limbs},{{missing arms}},{missing fingers},"
    "{{missing legs}},{{{more than 2 nipples}}},mutated hands,{{{mutation}}},normal quality,owres,"
    "{{poorly drawn face}},{{poorly drawn hands}},reen eyes,signature,text,{{too many fingers}},"
    "{{{ugly}}},username,uta,watermark,worst quality,{{{more than 2 legs}}},"
    "awkward hand sign,weird hand gesture,contorted hand,unnatural finger pose,deformed hand gesture,"
    "{shaka},{hang loose},{{rock on}},{shaka sign}"
)
NAI_TOOL_PROMPT_INSTRUCTIONS = (
    "使用英文逗号分隔标签。必须完整描述主体数量、全身或半身范围、姿态、镜头距离、"
    "视角、背景、光照和画面边界，避免残图；不得改变用户明确指定的主体、数量、动作和服装。"
)

PROVIDER_TRANSPORT_DEFAULTS: dict[str, dict[str, str]] = {
    "openai_images": {
        "base_url": "https://api.openai.com/v1",
        "generate_path": "/images/generations",
        "edit_path": "/images/edits",
        "edit_request_format": "multipart",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com",
        "generate_path": "/v1beta/models/{model}:generateContent",
        "edit_path": "/v1beta/models/{model}:generateContent",
        "edit_request_format": "json_data_url",
    },
    "nai_direct": {
        "base_url": "https://nai.sta1n.cn",
        "generate_path": "/generate",
        "edit_path": "",
        "edit_request_format": "json_data_url",
    },
    "custom_json": {
        "base_url": "",
        "generate_path": "/v1/images/generations",
        "edit_path": "/v1/images/edits",
        "edit_request_format": "json_data_url",
    },
}


@dataclass(frozen=True, slots=True)
class ImageModel:
    """A model entry with model-specific capabilities and parameter schema."""

    id: str
    name: str
    text2img: bool
    img2img: bool
    negative_prompt: bool
    max_reference_images: int
    negative_prompt_default: str = ""
    parameters: dict[str, dict[str, Any]] = field(default_factory=dict)
    tool: dict[str, Any] = field(default_factory=dict)
    capability_source: str = "manual"

    def supports(self, mode: GenerationMode) -> bool:
        """Return whether the model supports a requested generation mode."""

        return (
            self.text2img
            if mode == "text2img"
            else self.img2img and self.max_reference_images > 0
        )

    def public_dict(self) -> dict[str, Any]:
        """Return the model descriptor consumed by the WebUI."""

        return {
            "id": self.id,
            "name": self.name,
            "supports_text2img": self.text2img,
            "supports_img2img": self.img2img,
            "supports_negative_prompt": self.negative_prompt,
            "negative_prompt_default": self.negative_prompt_default,
            "max_reference_images": self.max_reference_images,
            "parameters": self.parameters,
            "tool": self.tool,
            "capability_source": self.capability_source,
        }

    @property
    def llm_enabled(self) -> bool:
        """Whether this model is exposed to the LLM image tools."""

        return _as_bool(self.tool.get("enabled"), True)

    @property
    def llm_max_reference_images(self) -> int:
        """Return the LLM-specific reference limit within model capability."""

        if not self.img2img:
            return 0
        configured = _as_int(
            self.tool.get("max_reference_images"), self.max_reference_images
        )
        configured = max(0, min(8, configured))
        return max(0, min(self.max_reference_images, configured))


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Declared image-generation abilities for one provider.

    Args:
        text2img: Whether text-to-image requests are accepted.
        img2img: Whether image-to-image requests are accepted.
        max_reference_images: Maximum images accepted by image-to-image.
        negative_prompt: Whether the provider accepts a dedicated negative prompt.
    """

    text2img: bool = True
    img2img: bool = False
    max_reference_images: int = 0
    negative_prompt: bool = False

    def supports(self, mode: GenerationMode) -> bool:
        """Return whether the provider supports a requested mode."""

        return self.text2img if mode == "text2img" else self.img2img


@dataclass(frozen=True, slots=True)
class ImageProvider:
    """Normalized persisted provider configuration.

    API keys remain in this value because the plugin WebUI is deliberately the
    single-user configuration surface. They are never copied to history or logs.
    """

    id: str
    name: str
    enabled: bool
    kind: str
    base_url: str
    generate_path: str
    edit_path: str
    model: str
    api_key: str
    custom_headers: str
    timeout_seconds: int
    capabilities: ProviderCapabilities
    models: tuple[ImageModel, ...] = ()
    edit_request_format: str = "multipart"
    request_template: str = ""
    response_image_path: str = ""
    max_concurrent_generations: int = 2
    models_path: str = ""
    discovered_models: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> ImageProvider:
        """Create a bounded provider value from persisted configuration."""

        kind = _text(value.get("kind"), 32) or "openai_images"
        transport_defaults = PROVIDER_TRANSPORT_DEFAULTS.get(
            kind, PROVIDER_TRANSPORT_DEFAULTS["custom_json"]
        )
        legacy_model_id = _text(value.get("model"), 160)
        max_refs = max(1, min(8, _as_int(value.get("max_reference_images"), 1)))
        img2img = kind != "nai_direct" and _as_bool(
            value.get("supports_img2img"), False
        )
        legacy_model = ImageModel(
            id=legacy_model_id,
            name=legacy_model_id,
            text2img=_as_bool(value.get("supports_text2img"), True),
            img2img=img2img,
            negative_prompt=(
                False
                if kind == "gemini"
                else _as_bool(
                    value.get("supports_negative_prompt"), kind == "nai_direct"
                )
            ),
            max_reference_images=max_refs if img2img else 0,
            negative_prompt_default=_model_negative_default(value, kind),
            parameters=_normalize_parameters(value.get("parameters")),
            tool=_normalize_tool(
                value.get("tool"), img2img, max_refs if img2img else 0, kind
            ),
            capability_source=_text(value.get("capability_source"), 32) or "manual",
        )
        raw_models = value.get("models")
        models = (
            tuple(
                _model_from_mapping(item, kind)
                for item in raw_models[:32]
                if isinstance(item, dict) and _text(item.get("id"), 160)
            )
            if isinstance(raw_models, list)
            else ()
        )
        if not models and legacy_model_id:
            models = (legacy_model,)
        primary_model = models[0] if models else legacy_model
        return cls(
            id=_text(value.get("id"), 64),
            name=_text(value.get("name"), 96),
            enabled=_as_bool(value.get("enabled"), True),
            kind=kind,
            base_url=_text(
                value.get("base_url") or transport_defaults["base_url"], 500
            ).rstrip("/"),
            generate_path=_normalized_path(
                value.get("generate_path"), transport_defaults["generate_path"]
            ),
            edit_path=_normalized_path(
                value.get("edit_path"), transport_defaults["edit_path"]
            ),
            model=primary_model.id,
            api_key=str(value.get("api_key") or ""),
            custom_headers=str(value.get("custom_headers") or ""),
            timeout_seconds=max(
                15, min(600, _as_int(value.get("timeout_seconds"), 180))
            ),
            capabilities=ProviderCapabilities(
                text2img=any(item.text2img for item in models),
                img2img=any(item.supports("img2img") for item in models),
                max_reference_images=max(
                    (item.max_reference_images for item in models), default=0
                ),
                negative_prompt=any(item.negative_prompt for item in models),
            ),
            models=models,
            edit_request_format=_text(value.get("edit_request_format"), 24)
            or transport_defaults["edit_request_format"],
            request_template=str(value.get("request_template") or ""),
            response_image_path=_text(value.get("response_image_path"), 240),
            max_concurrent_generations=max(
                1, min(16, _as_int(value.get("max_concurrent_generations"), 2))
            ),
            models_path=_normalized_path(
                value.get("models_path"),
                "/v1beta/models" if kind == "gemini" else "/models",
            ),
            discovered_models=_normalize_discovered_models(
                value.get("discovered_models")
            ),
        )

    def public_dict(self) -> dict[str, Any]:
        """Return provider data used by the explicitly single-user WebUI."""

        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "kind": self.kind,
            "base_url": self.base_url,
            "generate_path": self.generate_path,
            "edit_path": self.edit_path,
            "model": self.model,
            "api_key": self.api_key,
            "custom_headers": self.custom_headers,
            "timeout_seconds": self.timeout_seconds,
            "supports_text2img": self.capabilities.text2img,
            "supports_img2img": self.capabilities.img2img,
            "max_reference_images": self.capabilities.max_reference_images,
            "supports_negative_prompt": self.capabilities.negative_prompt,
            "edit_request_format": self.edit_request_format,
            "request_template": self.request_template,
            "response_image_path": self.response_image_path,
            "max_concurrent_generations": self.max_concurrent_generations,
            "models_path": self.models_path,
            "discovered_models": list(self.discovered_models),
            "models": [item.public_dict() for item in self.models],
        }

    def get_model(self, model_id: str = "") -> ImageModel:
        """Return a configured model, falling back to the provider's first model."""

        requested = str(model_id or "").strip()
        for model in self.models:
            if model.id == requested:
                return model
        return (
            self.models[0]
            if self.models
            else ImageModel(
                id=self.model,
                name=self.model,
                text2img=self.capabilities.text2img,
                img2img=self.capabilities.img2img,
                negative_prompt=self.capabilities.negative_prompt,
                max_reference_images=self.capabilities.max_reference_images,
                tool={"enabled": True},
            )
        )


@dataclass(frozen=True, slots=True)
class ReferenceImage:
    """An image supplied as a generation reference."""

    id: str
    filename: str
    data: bytes
    mime_type: str


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """A validated normalized image generation request."""

    mode: GenerationMode
    provider_id: str
    prompt: str
    negative_prompt: str = ""
    model: str = ""
    size: str = ""
    count: int = 1
    parameters: dict[str, Any] = field(default_factory=dict)
    references: tuple[ReferenceImage, ...] = ()
    source: str = "webui"
    selection_source: str = "fallback"


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    """A normalized image returned by any provider."""

    data: bytes
    mime_type: str


@dataclass(frozen=True, slots=True)
class GenerationResult:
    """Result from the image service before presentation to a caller."""

    provider: ImageProvider
    request: GenerationRequest
    images: tuple[GeneratedImage, ...]
    elapsed_ms: int
    generation_id: str = ""
    error: str = ""


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
            "enabled",
            "enable",
            "开启",
        }
    return default if value is None else bool(value)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any, limit: int) -> str:
    return str(value or "").replace("\x00", " ").strip()[:limit]


def _normalized_path(value: Any, default: str) -> str:
    path = _text(value, 300) or default
    if not path:
        return ""
    if path.startswith(("http://", "https://")):
        return path
    return path if path.startswith("/") else f"/{path}"


def _normalize_parameters(value: Any) -> dict[str, dict[str, Any]]:
    """Normalize model parameter descriptors while preserving unknown fields."""

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = None
    if not isinstance(value, dict):
        return dict(COMMON_MODEL_PARAMETERS)
    result: dict[str, dict[str, Any]] = {}
    for key, descriptor in value.items():
        name = _text(key, 64)
        if not name or not isinstance(descriptor, dict):
            continue
        item = dict(descriptor)
        item["type"] = _text(item.get("type"), 16) or "text"
        item["label"] = _text(item.get("label"), 96) or name
        item["request_key"] = _text(item.get("request_key"), 96) or name
        result[name] = item
    if not result:
        return dict(COMMON_MODEL_PARAMETERS)
    return result


def _model_from_mapping(value: dict[str, Any], kind: str) -> ImageModel:
    """Create a model descriptor from a provider-owned model mapping."""

    model_id = _text(value.get("id"), 160)
    img2img = kind != "nai_direct" and _as_bool(value.get("supports_img2img"), False)
    capability_source = _text(value.get("capability_source"), 32) or "manual"
    max_reference_images = (
        max(0, min(8, _as_int(value.get("max_reference_images"), 0))) if img2img else 0
    )
    return ImageModel(
        id=model_id,
        name=_text(value.get("name"), 160) or model_id,
        text2img=_as_bool(value.get("supports_text2img"), True),
        img2img=img2img,
        negative_prompt=(
            False
            if kind == "gemini"
            else _as_bool(value.get("supports_negative_prompt"), kind == "nai_direct")
        ),
        max_reference_images=max_reference_images,
        negative_prompt_default=_model_negative_default(value, kind),
        parameters=_normalize_parameters(value.get("parameters")),
        tool=_normalize_tool(value.get("tool"), img2img, max_reference_images, kind),
        capability_source=capability_source,
    )


def _normalize_tool(
    value: Any, img2img: bool, max_reference_images: int, kind: str
) -> dict[str, Any]:
    """Normalize the LLM-facing model policy without changing model schema."""

    raw = value if isinstance(value, dict) else {}
    parameters = raw.get("parameters")
    normalized_parameters: dict[str, dict[str, Any]] = {}
    if isinstance(parameters, dict):
        for key, descriptor in parameters.items():
            name = _text(key, 64)
            if name and isinstance(descriptor, dict):
                normalized_parameters[name] = dict(descriptor)
    limit_default = max_reference_images if img2img else 0
    nai = kind == "nai_direct"
    return {
        "enabled": _as_bool(raw.get("enabled", raw.get("available_to_llm")), True),
        "selection_description": _text(raw.get("selection_description"), 1200)
        or ("仅在用户明确要求 NAI 或 NovelAI 风格标签生图时使用。" if nai else ""),
        "prompt_profile": _text(raw.get("prompt_profile"), 48)
        or ("nai_tags" if nai else "natural_language"),
        "prompt_instructions": _text(raw.get("prompt_instructions"), 4000)
        or (NAI_TOOL_PROMPT_INSTRUCTIONS if nai else ""),
        "max_reference_images": max(
            0, min(8, _as_int(raw.get("max_reference_images"), limit_default))
        )
        if img2img
        else 0,
        "parameters": normalized_parameters,
    }


def _normalize_discovered_models(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value[:128]:
        if not isinstance(item, dict):
            continue
        model_id = _text(item.get("id"), 160)
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        result.append(
            {
                "id": model_id,
                "name": _text(item.get("name"), 160) or model_id,
                "supports_text2img": bool(item.get("supports_text2img")),
                "supports_img2img": bool(item.get("supports_img2img")),
                "supports_negative_prompt": bool(item.get("supports_negative_prompt")),
                "max_reference_images": max(
                    0, min(8, _as_int(item.get("max_reference_images"), 0))
                ),
                "capability_source": _text(item.get("capability_source"), 32)
                or "unknown",
            }
        )
    return tuple(result)


def _model_negative_default(value: dict[str, Any], kind: str) -> str:
    configured = (
        value.get("negative_prompt_default")
        if "negative_prompt_default" in value
        else NAI_DEFAULT_NEGATIVE
        if kind == "nai_direct"
        else ""
    )
    return _text(configured, 4000)
