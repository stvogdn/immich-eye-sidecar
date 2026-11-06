# immich-eye-sidecar

Runs **Desert Ant Labs' Eye** photo model over an **Immich** library and writes
the results back as namespaced tags (`eye:dog`, `eye:screenshot`,
`eye:eyes_closed`, `eye:blurry`, `eye:sensitive`, …).

Immich has no plugin path for third-party models — its ML service runs CLIP,
face recognition and OCR only — so this is an external consumer: it reads assets
through the Immich API, pulls each asset's preview JPEG, shells out to the
`desertant` CLI in batches, and attaches tags through the tags API.

**It never deletes, archives, rates or untags anything.** The only write is
adding tags whose name starts with the configured prefix.

Status: **beta**, built and running against a 110k-asset library. Written
because no Immich ↔ Eye integration existed; MIT-licensed, issues welcome.
The CLI and model weights are Desert Ant Labs' own — see
[THIRD_PARTY.md](THIRD_PARTY.md).

## Requirements

| Piece | Where | Why |
|---|---|---|
| `desertant` CLI ≥ 0.5.0 | `curl -fsSL https://raw.githubusercontent.com/Desert-Ant-Labs/desert-ant-cli/main/install.sh \| sh` | runs Eye; weights (~66 MB LiteRT / 48 MB Core ML) download once |
| `ffmpeg` | `apt install ffmpeg` / `brew install ffmpeg` | the CLI decodes JPEG/PNG frames with it; without it every file errors "needs ffmpeg" |
| Python ≥ 3.11 | system python is fine | the sidecar itself is stdlib-only |
| Immich API key | Immich → Account Settings → API Keys | reads assets/previews, writes tags |

## Run

```bash
export IMMICH_URL=https://immich.example.com
export IMMICH_API_KEY_FILE=/etc/immich-eye/api-key
export STATE_DB=/var/lib/immich-eye/state.db
export PATH="$HOME/.local/bin:$PATH"

./eye_sidecar.py --dry-run --limit 5      # analyse 5 assets, write nothing
./eye_sidecar.py --limit 5                # real writes
./eye_sidecar.py                          # walk the whole library, incremental
./eye_sidecar.py --report                 # state totals + recent failures
```

## Quick start on any host

```bash
# 1. the CLI + a decoder
curl -fsSL https://raw.githubusercontent.com/Desert-Ant-Labs/desert-ant-cli/main/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
sudo apt install -y ffmpeg          # or: brew install ffmpeg

# 2. prove the model runs before involving Immich
desertant eye some-photo.jpg --json

# 3. point the sidecar at your server
git clone https://github.com/stvogdn/immich-eye-sidecar && cd immich-eye-sidecar
export IMMICH_URL=https://immich.example.com
export IMMICH_API_KEY=...           # Immich -> Account Settings -> API Keys
export STATE_DB=./state.db
./eye_sidecar.py --dry-run --limit 5
./eye_sidecar.py --limit 5          # then drop --limit when it looks right
```

Run it from anywhere that can reach the Immich API. Running it *on* the Immich
host (as below) just saves the preview round trip.

## What gets tagged

| Group | Tags | Cutoff |
|---|---|---|
| What the file is | `screenshot`, `photo_of_screen`, `receipt`, `document`, `id_card`, `whiteboard`, `map`, `chart_or_slide`, `meme_or_graphic`, `illustration_or_art`, `shopping_reference`, `medical_reference`, `repair_reference` | 0.5 |
| Quality / people | `blurry`, `rotated`, `eyes_closed`, `everyone_smiling`, `group_shot`, `portrait` | 0.5 |
| Keeper | `keeper` (model keeper score ≥ 0.8, and not blurry) | score |
| Sensitive | `sensitive`, `nsfw_explicit`, `nsfw_suggestive`, `blood_injury`, `medical_hospital`, `funeral`, `accident_disaster`, `violence_protest`, `weapon` | **0.9** |
| Scenes | the top `--max-scene-tags` (default 8) of the 558 Open Images classes — `dog`, `beach`, `food`, `city`, … | 0.5 |

Deliberately **not** written: `photo`, `sharp`, `people`, `keeper`/`forgettable`
as a keep-or-delete verdict, `reference`, `low_content` — they are always-true
or judgement calls, and a sidecar should not make those.

## Options

```
--dry-run              analyse, write nothing (state is not updated)
--force                re-process assets whose updatedAt has not changed
--limit N              stop after N processed assets
--since ISO            only assets created after this date
--batch N              files per desertant invocation (default 24)
--page-size N          assets per Immich search page (default 200)
--max-pages N
--max-scene-tags N     scene tags per asset (default 8)
--no-scenes            kind + quality + sensitive tags only
--prefix NAME          tag namespace (default "eye")
--report               print state totals and recent failures
```

Environment: `IMMICH_URL`, `IMMICH_API_KEY` or `IMMICH_API_KEY_FILE`,
`DESERTANT_BIN`, `STATE_DB`, `TAG_PREFIX`, `BATCH`, `PAGE_SIZE`,
`MAX_SCENE_TAGS`, `DRY_RUN=1`, `KUMA_PUSH_URL` (optional Uptime Kuma push
monitor; a failed run pushes `down`).

## How it stays cheap and idempotent

- **Batching.** Model load dominates inference (≈3 s per process vs ≈0.3 s per
  photo), so each `desertant eye a.jpg b.jpg … --json` call covers a whole batch
  and returns one JSON object per file.
- **State.** SQLite records `asset_id → checksum, tags, scores`. An asset is
  skipped when its checksum matches, so a pass only touches new or replaced
  photos. Do not key on `updatedAt`: writing a tag bumps it in Immich, so an
  `updatedAt` key makes every asset look new again forever. `--force` overrides.
- **Bulk tag writes.** Assets are grouped per tag and attached with one
  `PUT /api/tags/{id}/assets` per tag batch, not one call per asset.

## Deploy (on the host where Immich runs)

```bash
sudo mkdir -p /opt/immich-eye /var/lib/immich-eye
sudo install -m 644 eye_sidecar.py /opt/immich-eye/
sudo install -m 600 /dev/stdin /etc/immich-eye/api-key   # paste the key, then Ctrl-D
sudo install -m 644 deploy/immich-eye-sidecar.service deploy/immich-eye-sidecar.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now immich-eye-sidecar.timer
sudo journalctl -u immich-eye-sidecar -n 50 --no-pager
```

The unit runs as the user that installed `desertant`, so it inherits
`~/.local/bin/desertant` and the cached model weights; `STATE_DB=/var/lib/immich-eye/state.db`
must be writable by that user. Set `User=`/`Group=` in the unit to match. `README`
ships with `User=immich-eye`; change it to your own account if the CLI lives in
your home directory.

## Throughput (measured on an x86 host, 2026-11-06)

- **≈2.75 s per photo** single-threaded, CPU only (LiteRT build, no GPU): 40
  photos in 110 s end to end, including Immich API round trips and 60-ish tag
  writes.
- The timer runs a pass every 20 minutes with `--limit 400`, which is ~18
  minutes of work per pass — about **1,200 photos/hour**, so a 30k-image
  library drains in roughly a day of background work, newest first.
- Model load is ~3 s per invocation, so keep `--batch` well above 1: batches of
  24 amortise it to nothing.

## Licence

The sidecar's own code is MIT (see [LICENSE](LICENSE)). It drives Desert Ant
Labs' CLI and weights, which have their own licence — [THIRD_PARTY.md](THIRD_PARTY.md).

## Tests

```bash
cd tests && python3 -m unittest -v
```

## Notes and limits

- Only `type: IMAGE` assets are searched; videos are skipped (Eye samples video
  frames, but that needs the original, not a preview).
- Previews are the model's input, so a 1.5 MP preview is plenty — Eye resizes
  internally. Larger inputs cost time, not accuracy.
- `KEEP_ALIVE`/rate limits: Immich serves previews from its own cache; a
  full pass over a large library is mostly disk reads on the Immich host.
- If `desertant` starts returning non-zero, check `ffmpeg -version` first: the
  binary is installed but image decoding fails without it.
