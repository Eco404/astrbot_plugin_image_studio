# Image Studio

`astrbot_plugin_image_gen` provides image generation for AstrBot commands, LLM tools, and a three-area WebUI:

- Generate: text-to-image and image-to-image with capability-filtered Providers.
- Gallery: searchable history, parameter review, reproduction drafts, export, and batch deletion.
- Settings: Provider, history, and runtime settings stored in AstrBot's plugin configuration.

## Install

Place this directory at `AstrBot/data/plugins/astrbot_plugin_image_gen`, enable the plugin, then open the Image Studio Page from the plugin detail view.

The AstrBot native plugin settings page intentionally exposes only the bootstrap controls. The WebUI writes all Provider and history settings to the same AstrBot plugin configuration file under the hidden `webui_managed` group.

## Provider kinds

The settings page supports these initial adapters:

- `openai_images`: OpenAI Images API and compatible `/images/generations` and `/images/edits` services.
- `gemini`: Gemini `generateContent` image output with inline reference images.
- `nai_direct`: NAI-compatible `GET /generate` services for text-to-image.
- `custom_json`: Custom JSON request body and response extraction for compatible gateways.

Each Provider declares text-to-image and image-to-image capabilities. A disabled, incomplete, or unsupported Provider is not selectable for the active generation mode.

## Commands and Agent tool

Use the fixed commands:

```text
/image_gen a studio photograph of a mountain lake --provider my-openai --size 1024x1024
/image_gen repaint this image --mode img2img --ref /path/from/astrbot/temp/tool_images/file.png
```

`image_gen_generate` returns MCP `ImageContent`, not only a text path. AstrBot caches that image and sends it into the next visual-capable Agent step, allowing the Agent to inspect or process it before deciding whether to send it to the user.

## Data boundaries

- Plugin configuration, including visible WebUI API keys, remains in AstrBot's plugin configuration file.
- Gallery records are stored in `data/plugin_data/astrbot_plugin_image_gen/history.sqlite3`.
- Result images and retained reference images are plugin-owned files below the same data directory.
- Reference images are displayed only inside a generation's detail view. Deleting one reference does not delete its generated result or parameter record.
- API keys, authorization headers, and credential-like parameter names are removed from gallery history and export manifests.
