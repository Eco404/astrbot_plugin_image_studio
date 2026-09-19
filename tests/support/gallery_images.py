"""Reusable isolated gallery images fixtures; no test-module imports."""

from __future__ import annotations


import io

import json


from PIL import Image, PngImagePlugin


def image(color="red", *, prompt="embedded", model="embedded-model", seed=1):
    output = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text("Software", "NovelAI")
    info.add_text(
        "Comment",
        json.dumps({"prompt": prompt, "model": model, "seed": seed, "uc": "blur"}),
    )
    Image.new("RGB", (24, 32), color).save(output, "PNG", pnginfo=info)
    return output.getvalue()


def stage(store, data, name, **overrides):
    path = store.imports_dir / name
    path.write_bytes(data)
    return {"path": path, "filename": name, "overrides": overrides}


def comfy_multi_output_image(color="red"):
    graph = {}
    for offset, prompt in ((0, "first branch prompt"), (10, "second branch prompt")):
        graph.update(
            {
                str(offset + 1): {
                    "class_type": "CLIPTextEncode",
                    "inputs": {"text": prompt},
                },
                str(offset + 2): {
                    "class_type": "CLIPTextEncode",
                    "inputs": {"text": "negative " + prompt},
                },
                str(offset + 3): {
                    "class_type": "EmptyLatentImage",
                    "inputs": {"width": 512, "height": 512},
                },
                str(offset + 4): {
                    "class_type": "KSampler",
                    "inputs": {
                        "positive": [str(offset + 1), 0],
                        "negative": [str(offset + 2), 0],
                        "latent_image": [str(offset + 3), 0],
                        "steps": offset + 20,
                        "seed": offset + 1,
                    },
                },
                str(offset + 5): {
                    "class_type": "VAEDecode",
                    "inputs": {"samples": [str(offset + 4), 0]},
                },
                str(offset + 6): {
                    "class_type": "SaveImage",
                    "inputs": {"images": [str(offset + 5), 0]},
                },
            }
        )
    graph["99"] = {"class_type": "PreviewImage", "inputs": {"images": ["5", 0]}}
    raw = {
        "prompt": json.dumps(graph),
        "workflow": json.dumps({"nodes": [], "links": []}),
    }
    info = PngImagePlugin.PngInfo()
    for key, value in raw.items():
        info.add_text(key, value)
    output = io.BytesIO()
    Image.new("RGB", (24, 32), color).save(output, "PNG", pnginfo=info)
    return output.getvalue(), raw
