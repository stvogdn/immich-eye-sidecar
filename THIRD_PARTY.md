# Third-party components

This project is a thin driver. Nothing third-party is vendored, bundled or
redistributed here — you install it yourself.

| Component | What it is | Where it comes from | Terms |
|---|---|---|---|
| **Desert Ant Labs CLI** (`desertant`) | runs the Eye model; this sidecar shells out to `desertant eye … --json` | https://github.com/Desert-Ant-Labs/desert-ant-cli | Desert Ant Labs' own licence (source-available, not MIT). Read their LICENSE.md and terms at desertant.com before deploying |
| **Eye model weights** | ~66 MB LiteRT / ~48 MB Core ML, downloaded by the CLI on first run and cached | https://huggingface.co/desert-ant-labs/eye | Desert Ant Labs licence; their model page states free use up to a monthly-active-device threshold, with commercial terms above it |
| **Immich** | the photo server this talks to | https://immich.app | AGPL-3.0 (this sidecar is not a derivative of Immich; it only calls the HTTP API) |

Practical consequences for anyone reusing this:

- Do not vendor the CLI binary or the weights into your own repo or image.
- Do not assume the Eye licence matches this repo's MIT licence.
- This sidecar is an independent integration; it is not endorsed by Desert Ant
  Labs or the Immich project, and neither project reviewed it.

Tags written by this tool are ordinary Immich tags — if you publish screenshots
of a tagged library, check what the tags reveal about the photos first.
