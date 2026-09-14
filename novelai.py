"""NovelAI official wire protocol, separate from the third-party NAI gateway.

Parameter profiles follow the public image API and reviewed open-source clients.
Model defaults remain provisional until checked against a real account. No
account token or reference-image payload is included in effective parameters.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import math
import re
import secrets
import time
import zipfile
from typing import TYPE_CHECKING, Any

from PIL import Image, ImageOps, UnidentifiedImageError

if TYPE_CHECKING:
    from .models import GeneratedImage, GenerationRequest

NOVELAI_MODEL_IDS = (
    "nai-diffusion-4-5-full",
    "nai-diffusion-4-5-curated",
    "nai-diffusion-5-full",
    "nai-diffusion-5-curated",
)
MAX_RESPONSE_BYTES = 128 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_IMAGE_PIXELS = 64_000_000
MAX_RESPONSE_IMAGES = 64
MAX_NATIVE_SAMPLES = 16
MAX_SEED = 2**32 - 1
_BASE_PARAMETERS = {
    "steps": 28,
    "scale": 5.0,
    "cfg_rescale": 0.0,
    "sampler": "k_euler_ancestral",
    "noise_schedule": "karras",
    "image_format": "png",
}
_ALLOWED_PARAMETERS = {
    *_BASE_PARAMETERS,
    "seed",
    "params_version",
    "strength",
    "noise",
    "extra_noise_seed",
}
_CONTROL_PARAMETERS = {
    "size",
    "count",
    "n",
    "native_batch_size",
    "native_batch_size_source",
    "max_concurrent_requests",
}


def build_generation_payload(
    request: GenerationRequest, model_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a supported request and a safe snapshot of the effective inputs."""

    if model_id not in NOVELAI_MODEL_IDS:
        raise ValueError(f"NovelAI 官方适配器尚未支持模型 {model_id}")
    if request.mode not in {"text2img", "img2img"}:
        raise ValueError("NovelAI 官方接口当前仅支持文生图和单底图图生图")
    supplied = dict(request.parameters)
    unknown = sorted(set(supplied) - _ALLOWED_PARAMETERS - _CONTROL_PARAMETERS)
    if unknown:
        raise ValueError("NovelAI 官方接口不支持参数：" + "、".join(unknown))
    if request.mode == "text2img" and any(
        key in supplied for key in ("strength", "noise", "extra_noise_seed")
    ):
        raise ValueError("strength、noise 和 extra_noise_seed 仅用于图生图")
    if request.mode == "text2img" and request.references:
        raise ValueError("文生图不能包含底图；请使用图生图模式")
    if request.mode == "img2img" and len(request.references) != 1:
        raise ValueError("NovelAI 原生图生图需要且仅接受一张底图")
    size = str(request.size or "1024x1024").strip()
    match = re.fullmatch(r"([0-9]{1,5})[xX×]([0-9]{1,5})", size)
    if not match:
        raise ValueError("NovelAI 图片尺寸需为宽x高，例如 832x1216")
    width, height = map(int, match.groups())
    if min(width, height) < 64 or width % 64 or height % 64:
        raise ValueError("NovelAI 图片宽高必须为正的 64 倍数")
    if width * height > MAX_IMAGE_PIXELS:
        raise ValueError("NovelAI 图片尺寸超过插件像素上限")
    count = _integer(request.count, "count", 1, MAX_NATIVE_SAMPLES)
    parameters = {
        **_BASE_PARAMETERS,
        **{key: value for key, value in supplied.items() if key in _ALLOWED_PARAMETERS},
    }
    parameters["params_version"] = _integer(
        parameters.get(
            "params_version", 4 if model_id.startswith("nai-diffusion-5-") else 3
        ),
        "params_version",
        3,
        4,
    )
    parameters["seed"] = _seed(parameters.get("seed", -1), "seed")
    parameters["steps"] = _integer(parameters["steps"], "steps", 1, 1000)
    parameters["scale"] = _number(parameters["scale"], "scale", 0, 100)
    parameters["cfg_rescale"] = _number(parameters["cfg_rescale"], "cfg_rescale", 0, 1)
    for key in ("sampler", "noise_schedule"):
        value = parameters[key]
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,80}", value
        ):
            raise ValueError(f"NovelAI 参数 {key} 必须为有效名称")
    if parameters["image_format"] not in {"png", "webp"}:
        raise ValueError("NovelAI image_format 仅支持 png 或 webp")
    parameters.update(
        {
            "width": width,
            "height": height,
            "n_samples": count,
            "qualityToggle": False,
            "add_original_image": False,
            "negative_prompt": request.negative_prompt,
            "v4_prompt": {
                "caption": {"base_caption": request.prompt, "char_captions": []},
                "use_coords": False,
                "use_order": True,
            },
            "v4_negative_prompt": {
                "caption": {
                    "base_caption": request.negative_prompt,
                    "char_captions": [],
                },
                "legacy_uc": False,
            },
        }
    )
    if request.mode == "img2img":
        parameters["strength"] = _number(
            parameters.get("strength", 0.7), "strength", 0, 1
        )
        parameters["noise"] = _number(parameters.get("noise", 0), "noise", 0, 1)
        parameters["extra_noise_seed"] = _seed(
            parameters.get("extra_noise_seed", parameters["seed"]), "extra_noise_seed"
        )
        parameters["image"] = _reference_base64(
            request.references[0].data, width, height
        )
    effective = {
        key: value for key, value in parameters.items() if key in _ALLOWED_PARAMETERS
    }
    effective.update(
        {
            "size": f"{width}x{height}",
            "count": count,
        }
    )
    return {
        "input": request.prompt,
        "model": model_id,
        "action": "img2img" if request.mode == "img2img" else "generate",
        "parameters": parameters,
    }, effective


def parse_generation_response(
    raw: bytes,
    content_type: str,
    effective: dict[str, Any],
    *,
    expected_count: int | None = None,
) -> tuple[tuple[GeneratedImage, ...], tuple[tuple[int, str], ...]]:
    """Decode all images, retaining positional errors alongside successful ones."""

    from .models import GeneratedImage

    if not raw or len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("NovelAI 响应为空或超过 128 MB 上限")
    if "zip" in content_type.lower() or raw.startswith(b"PK\x03\x04"):
        return _parse_zip(raw, effective, expected_count=expected_count)
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("NovelAI 返回的不是有效 JSON 或 ZIP") from exc
    items = payload.get("images") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError("NovelAI 响应缺少非空 images 数组")
    if len(items) > MAX_RESPONSE_IMAGES:
        raise ValueError("NovelAI 响应图片数量超过插件上限")
    images: list[GeneratedImage] = []
    failures: list[tuple[int, str]] = []
    total = 0
    seen_indexes: set[int] = set()
    for position, item in enumerate(items, 1):
        try:
            if not isinstance(item, dict):
                raise ValueError("图片条目不是对象")  # noqa: TRY004 -- invalid wire data
            image_data = _decode_image(item.get("image"))
            total += len(image_data)
            if total > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("本次图片总大小超过 128 MB 上限")
            mime_type = _validate_image(image_data)
            actual = dict(effective)
            metadata_errors: list[str] = []
            try:
                index = _integer(item.get("index", position - 1), "index", 0, 2**31 - 1)
                if index in seen_indexes:
                    raise ValueError(f"重复的图片 index {index}")
                seen_indexes.add(index)
            except ValueError as exc:
                index = None
                metadata_errors.append(str(exc))
            if "seed" in item:
                try:
                    actual["seed"] = _integer(item["seed"], "seed", 0, MAX_SEED)
                except ValueError as exc:
                    actual.pop("seed", None)
                    metadata_errors.append(str(exc))
            elif len(items) > 1:
                # An input seed is not evidence for every image of a native batch.
                actual.pop("seed", None)
            images.append(
                GeneratedImage(
                    image_data,
                    mime_type,
                    effective_parameters=actual,
                    response_index=index,
                )
            )
            if metadata_errors:
                failures.append(
                    (
                        position,
                        "图片已保留，但返回参数无效：" + "；".join(metadata_errors),
                    )
                )
        except ValueError as exc:
            failures.append((position, str(exc)))
    failures.extend(_missing_response_failures(len(items), expected_count))
    return tuple(images), tuple(failures)


def parse_subscription(payload: Any) -> dict[str, Any]:
    """Keep subscription, purchased currency and recoverable usage separate."""

    if not isinstance(payload, dict) or not isinstance(payload.get("active"), bool):
        raise ValueError("NovelAI 额度响应缺少有效订阅状态")  # noqa: TRY004 -- invalid wire data
    balances = payload.get("trainingStepsLeft")
    balances = balances if isinstance(balances, dict) else {}
    subscription = _optional_nonnegative_integer(balances.get("fixedTrainingStepsLeft"))
    purchased = _optional_nonnegative_integer(balances.get("purchasedTrainingSteps"))
    raw_usage = payload.get("usage")
    usage = None
    if isinstance(raw_usage, dict):
        usage = {
            "percent": _optional_number(raw_usage.get("percent")),
            "is_negative": raw_usage.get("isNegative")
            if isinstance(raw_usage.get("isNegative"), bool)
            else None,
            "time_until_next_percent": _optional_nonnegative_number(
                raw_usage.get("timeUntilNextPercent")
            ),
        }
    return {
        "kind": "novelai_official",
        "subscription_active": payload["active"],
        "tier": _optional_nonnegative_integer(payload.get("tier")),
        "remaining": subscription + purchased
        if subscription is not None and purchased is not None
        else None,
        "subscription_anlas": subscription,
        "purchased_anlas": purchased,
        "usage": usage,
        "checked_at": time.time(),
    }


def _parse_zip(
    raw: bytes, effective: dict[str, Any], *, expected_count: int | None = None
):
    from .models import GeneratedImage

    images: list[GeneratedImage] = []
    failures: list[tuple[int, str]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = [item for item in archive.infolist() if not item.is_dir()]
            if not entries or len(entries) > MAX_RESPONSE_IMAGES:
                raise ValueError("NovelAI ZIP 条目数量为空或超过插件上限")
            if sum(item.file_size for item in entries) > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("NovelAI ZIP 解压总大小超过 128 MB 上限")
            for position, item in enumerate(entries, 1):
                try:
                    if item.file_size > MAX_IMAGE_BYTES:
                        raise ValueError("图片超过 30 MB 上限")
                    # Never extract archive names into the filesystem.
                    with archive.open(item) as stream:
                        data = stream.read(MAX_IMAGE_BYTES + 1)
                    mime_type = _validate_image(data)
                    actual = dict(effective)
                    if len(entries) > 1:
                        actual.pop("seed", None)
                    images.append(
                        GeneratedImage(
                            data,
                            mime_type,
                            effective_parameters=actual,
                            response_index=position - 1,
                        )
                    )
                except (
                    ValueError,
                    OSError,
                    RuntimeError,
                    zipfile.BadZipFile,
                    NotImplementedError,
                ) as exc:
                    reason = (
                        str(exc)
                        if isinstance(exc, ValueError)
                        else "ZIP 图片条目无法读取或校验失败"
                    )
                    failures.append((position, reason))
            failures.extend(_missing_response_failures(len(entries), expected_count))
    except zipfile.BadZipFile as exc:
        raise ValueError("NovelAI 返回的 ZIP 已损坏") from exc
    return tuple(images), tuple(failures)


def _decode_image(value: Any) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("图片 Base64 为空或类型无效")
    if value.startswith("data:"):
        header, separator, value = value.partition(",")
        if not separator or not re.fullmatch(
            r"data:image/[a-zA-Z0-9.+-]+;base64", header
        ):
            raise ValueError("图片 data URL 无效")
    if len(value) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
        raise ValueError("图片超过 30 MB 上限")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("图片 Base64 编码无效") from exc


def _validate_image(data: bytes) -> str:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("图片为空或超过 30 MB 上限")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("图片超过插件像素上限")
            if image.format not in {"PNG", "WEBP", "JPEG"}:
                raise ValueError("图片格式不受支持")
            mime_type = Image.MIME[image.format]
            image.verify()
        # verify() checks PNG checksums, but does not fully decode JPEG/WebP pixels.
        with Image.open(io.BytesIO(data)) as image:
            image.load()
        return mime_type
    except (
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        SyntaxError,
    ) as exc:
        raise ValueError("图片数据损坏或无法解码") from exc


def _reference_base64(data: bytes, width: int, height: int) -> str:
    _validate_image(data)
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            image = image.resize((width, height), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="PNG")
    except (OSError, ValueError) as exc:
        raise ValueError("NovelAI 图生图底图无法读取") from exc
    if output.tell() > MAX_IMAGE_BYTES:
        raise ValueError("NovelAI 图生图底图处理后超过 30 MB 上限")
    return base64.b64encode(output.getvalue()).decode("ascii")


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,20}", value.strip()):
        value = int(value.strip())
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"NovelAI 参数 {name} 必须为 {minimum}–{maximum} 的整数")
    return value


def _number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            pass
    if (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not minimum <= value <= maximum
        or not math.isfinite(value)
    ):
        raise ValueError(f"NovelAI 参数 {name} 必须为 {minimum}–{maximum} 的数值")
    return value


def _seed(value: Any, name: str) -> int:
    seed = _integer(value, name, -1, MAX_SEED)
    return secrets.randbelow(MAX_SEED + 1) if seed == -1 else seed


def _optional_number(value: Any) -> int | float | None:
    try:
        if type(value) in (int, float) and math.isfinite(value):
            return value
    except OverflowError:
        pass
    return None


def _optional_nonnegative_number(value: Any) -> int | float | None:
    parsed = _optional_number(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _optional_nonnegative_integer(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value < 2**63 else None


def _missing_response_failures(
    actual: int, expected: int | None
) -> list[tuple[int, str]]:
    if expected is None or actual >= expected:
        return []
    return [
        (index, "上游未返回本次请求中的这张图片")
        for index in range(actual + 1, expected + 1)
    ]
