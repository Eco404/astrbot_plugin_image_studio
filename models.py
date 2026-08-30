"""Value objects shared by Image Studio services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


GenerationMode = Literal["text2img", "img2img"]


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
    edit_request_format: str = "multipart"
    request_template: str = ""
    response_image_path: str = ""

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "ImageProvider":
        """Create a bounded provider value from persisted configuration."""

        kind = _text(value.get("kind"), 32) or "openai_images"
        max_refs = max(0, min(8, _as_int(value.get("max_reference_images"), 1)))
        img2img = _as_bool(value.get("supports_img2img"), False)
        return cls(
            id=_text(value.get("id"), 64),
            name=_text(value.get("name"), 96),
            enabled=_as_bool(value.get("enabled"), True),
            kind=kind,
            base_url=_text(value.get("base_url"), 500).rstrip("/"),
            generate_path=_normalized_path(
                value.get("generate_path"), "/v1/images/generations"
            ),
            edit_path=_normalized_path(value.get("edit_path"), "/v1/images/edits"),
            model=_text(value.get("model"), 160),
            api_key=str(value.get("api_key") or ""),
            custom_headers=str(value.get("custom_headers") or ""),
            timeout_seconds=max(
                15, min(600, _as_int(value.get("timeout_seconds"), 180))
            ),
            capabilities=ProviderCapabilities(
                text2img=_as_bool(value.get("supports_text2img"), True),
                img2img=img2img,
                max_reference_images=max_refs if img2img else 0,
                negative_prompt=_as_bool(
                    value.get("supports_negative_prompt"), kind == "nai_direct"
                ),
            ),
            edit_request_format=_text(value.get("edit_request_format"), 24)
            or "multipart",
            request_template=str(value.get("request_template") or ""),
            response_image_path=_text(value.get("response_image_path"), 240),
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
        }


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
    return path if path.startswith("/") else f"/{path}"
