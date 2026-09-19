"""Small synthetic import images; no personal gallery files or host APIs needed.

CLI: python tests/fixtures/image_metadata.py comfyui-webp unique-test-id
The image is written to stdout for Playwright's in-memory file upload API.
"""

from __future__ import annotations

import argparse
import io
import json
import sys

from PIL import Image, PngImagePlugin


def comfyui_metadata() -> dict[str, str]:
    """A standard seven-node text-to-image graph with a UI export to download."""
    graph = {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "synthetic-model.safetensors"},
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "synthetic mountain landscape", "clip": ["1", 1]},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "low quality", "clip": ["1", 1]},
        },
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": 96, "height": 128, "batch_size": 1},
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
                "seed": 1234,
                "steps": 20,
                "cfg": 7,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1,
            },
        },
        "6": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
        },
        "7": {
            "class_type": "SaveImage",
            "inputs": {"images": ["6", 0], "filename_prefix": "synthetic"},
        },
    }
    nodes, links = [], []
    for key, node in graph.items():
        inputs, widgets = [], []
        for name, value in node["inputs"].items():
            if isinstance(value, list):
                link_id = len(links) + 1
                links.append(
                    [link_id, int(value[0]), value[1], int(key), len(inputs), "*"]
                )
                inputs.append({"name": name, "type": "*", "link": link_id})
            else:
                widgets.append(value)
        if node["class_type"] == "KSampler":
            # Frontend-only seed behavior occupies the second widget slot.
            widgets.insert(1, "fixed")
        nodes.append(
            {
                "id": int(key),
                "type": node["class_type"],
                "inputs": inputs,
                "widgets_values": widgets,
            }
        )
    workflow = {"version": 0.4, "nodes": nodes, "links": links}
    return {"prompt": json.dumps(graph), "workflow": json.dumps(workflow)}


def image_bytes(kind: str, nonce: str = "fixture") -> bytes:
    image = Image.new("RGB", (96, 128), (112, 152, 173))
    if kind == "novelai":
        fields = {
            "Software": "NovelAI",
            "Comment": json.dumps(
                {
                    "prompt": "synthetic mountains",
                    "uc": "low quality",
                    "model": "nai-diffusion-4-5-full",
                    "steps": 20,
                    "scale": 5,
                    "seed": 1234,
                    "sampler": "k_euler",
                    "width": 96,
                    "height": 128,
                }
            ),
        }
    elif kind.startswith("comfyui"):
        fields = comfyui_metadata()
    elif kind == "a1111":
        fields = {
            "parameters": "synthetic mountains\nNegative prompt: low quality\nSteps: 20, Sampler: Euler, CFG scale: 7, Seed: 1234, Size: 96x128, Model: synthetic-model"
        }
    else:
        raise ValueError(f"Unsupported synthetic image kind: {kind}")
    output = io.BytesIO()
    if kind.endswith("-webp") or kind == "a1111":
        exif = Image.Exif()
        exif[270] = nonce
        if kind.startswith("comfyui"):
            exif[270] = "Workflow: " + fields["workflow"]
            exif[271] = "Prompt: " + fields["prompt"]
            exif[305] = nonce
        else:
            exif[37510] = b"ASCII\0\0\0" + fields["parameters"].encode("utf-8")
        image.save(output, "WEBP", lossless=True, exif=exif)
    else:
        metadata = PngImagePlugin.PngInfo()
        for key, value in {**fields, "BrowserFixture": nonce}.items():
            metadata.add_text(key, value)
        image.save(output, "PNG", pnginfo=metadata)
    return output.getvalue()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("novelai", "comfyui", "comfyui-webp", "a1111"))
    parser.add_argument("nonce", nargs="?", default="fixture")
    args = parser.parse_args()
    sys.stdout.buffer.write(image_bytes(args.kind, args.nonce))
