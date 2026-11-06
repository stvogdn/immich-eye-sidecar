#!/usr/bin/env python3
"""Eye → Immich sidecar.

Runs Desert Ant Labs' `desertant eye` over an Immich library and writes the
results back as namespaced tags. Nothing is deleted, archived or rated: the
only write is adding tags whose names start with the configured prefix.

Design notes
------------
* Immich has no plugin path for third-party models, so this is an external
  consumer: it reads assets through the Immich API, fetches each asset's
  preview JPEG, shells out to the `desertant` CLI in batches (one process per
  batch — model load is the expensive part, not inference) and then attaches
  tags through the tags API.
* Every write is idempotent: a local SQLite state file records the asset's
  `updatedAt` and the tag set that was written, so re-runs skip untouched
  assets and `--force` re-does them.
* Tag names are `<prefix>:<slug>` (default prefix `eye`), so the whole
  contribution can be found, filtered or stripped in one place.

Usage
-----
    eye_sidecar.py --once --limit 50 --dry-run
    eye_sidecar.py --once
    eye_sidecar.py --report
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Tag policy
# ---------------------------------------------------------------------------

# Confidence the model itself recommends for filters and search.
MIN_CONF = 0.5
# Anything that could hide or act without asking uses the strict cutoff.
STRICT_CONF = 0.9

# Tags that say what the file *is*. These are the ones people filter on.
KIND_TAGS = {
    "screenshot",
    "photo_of_screen",
    "receipt",
    "document",
    "id_card",
    "whiteboard",
    "map",
    "chart_or_slide",
    "meme_or_graphic",
    "illustration_or_art",
    "shopping_reference",
    "medical_reference",
    "repair_reference",
}

# Quality / people flags worth a tag.
QUALITY_FLAGS = (
    "blurry",
    "rotated",
    "eyes_closed",
    "everyone_smiling",
    "group_shot",
    "portrait",
)

# Never turned into tags: they are either always true, redundant with the
# other flags, or reintroduce a keep/delete decision this tool must not make.
CONTROL_TAGS = KIND_TAGS | {
    "photo",
    "sharp",
    "bokeh",
    "keeper",
    "forgettable",
    "reference",
    "reference_photo",
    "low_content",
    "accidental",
    "technical_flaw",
    "unusable",
    "bad_framing",
    "bad_lighting",
    "test_or_duplicate_shot",
    "peak_moment",
    "people",
    "everyone_looking",
    "candid",
    "smiling",
    "person",
    "sensitive",
    "nsfw_explicit",
    "nsfw_suggestive",
    "blood_injury",
    "medical_hospital",
    "funeral",
    "accident_disaster",
    "violence_protest",
    "weapon",
} | set(QUALITY_FLAGS)

# Sensitive flags: only ever applied at the strict cutoff.
SENSITIVE_TAGS = (
    "nsfw_explicit",
    "nsfw_suggestive",
    "blood_injury",
    "medical_hospital",
    "funeral",
    "accident_disaster",
    "violence_protest",
    "weapon",
)

KEEPER_SCORE = 0.8


def slug(name: str) -> str:
    """Turn a model tag into something safe for an Immich tag name."""
    out = []
    for ch in name.strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in "_- ":
            out.append("-" if ch != "_" else "_")
    s = "".join(out).strip("-")
    while "--" in s:
        s = s.replace("--", "-")
    return s


@dataclass
class TagPolicy:
    prefix: str = "eye"
    min_conf: float = MIN_CONF
    strict_conf: float = STRICT_CONF
    max_scene_tags: int = 8
    include_scenes: bool = True

    def name(self, tag: str) -> str:
        return f"{self.prefix}:{slug(tag)}"

    def select(self, result: dict) -> list[str]:
        """Return the full tag names this asset should carry, sorted."""
        tags: dict[str, float] = {
            str(k).lower(): float(v) for k, v in (result.get("tags") or {}).items()
        }
        chosen: set[str] = set()

        for tag, conf in tags.items():
            if tag in SENSITIVE_TAGS:
                if conf >= self.strict_conf:
                    chosen.add(self.name(tag))
                continue
            if tag == "sensitive":
                if conf >= self.strict_conf:
                    chosen.add(self.name("sensitive"))
                continue
            if tag in KIND_TAGS or tag in QUALITY_FLAGS:
                if conf >= self.min_conf:
                    chosen.add(self.name(tag))

        scores = result.get("scores") or {}
        keeper = scores.get("keeper")
        if keeper is not None and keeper >= KEEPER_SCORE and tags.get("blurry", 0.0) < self.min_conf:
            chosen.add(self.name("keeper"))

        if self.include_scenes and self.max_scene_tags > 0:
            scenes = [
                (t, c)
                for t, c in tags.items()
                if t not in CONTROL_TAGS and c >= self.min_conf
            ]
            scenes.sort(key=lambda tc: (-tc[1], tc[0]))
            for tag, _ in scenes[: self.max_scene_tags]:
                chosen.add(self.name(tag))

        return sorted(chosen)


# ---------------------------------------------------------------------------
# Immich API
# ---------------------------------------------------------------------------


class Immich:
    def __init__(self, base_url: str, api_key: str, timeout: float = 60.0):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.timeout = timeout
        self._tag_cache: dict[str, str] = {}

    def _request(self, method: str, path: str, body: dict | None = None,
                 raw: bool = False, retries: int = 3) -> "Any":
        url = f"{self.base}{path}"
        data = None
        headers = {"x-api-key": self.key, "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        last: Exception | None = None
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = resp.read()
                    return payload if raw else (json.loads(payload) if payload else None)
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:300].decode("utf-8", "replace")
                if exc.code in (429, 500, 502, 503, 504) and attempt + 1 < retries:
                    time.sleep(2 * (attempt + 1))
                    last = RuntimeError(f"{method} {path} -> {exc.code}: {detail}")
                    continue
                raise RuntimeError(f"{method} {path} -> {exc.code}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last = exc
                if attempt + 1 < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError(f"{method} {path} -> {exc}") from exc
        raise RuntimeError(f"{method} {path} failed: {last}")

    # -- reads -------------------------------------------------------------
    def version(self) -> str:
        v = self._request("GET", "/api/server/version")
        return ".".join(str(v.get(k)) for k in ("major", "minor", "patch"))

    def search_images(self, page: int, size: int) -> dict:
        return self._request(
            "POST", "/api/search/metadata",
            {"page": page, "size": size, "type": "IMAGE", "withExif": False},
        )["assets"]

    def preview(self, asset_id: str) -> bytes:
        return self._request("GET", f"/api/assets/{asset_id}/thumbnail?size=preview", raw=True)

    def tags(self) -> list[dict]:
        return self._request("GET", "/api/tags")

    # -- writes (tags only) -------------------------------------------------
    def ensure_tag(self, name: str) -> str:
        if name in self._tag_cache:
            return self._tag_cache[name]
        for tag in self.tags():
            self._tag_cache[tag["name"]] = tag["id"]
        if name in self._tag_cache:
            return self._tag_cache[name]
        created = self._request("POST", "/api/tags", {"name": name})
        self._tag_cache[created["name"]] = created["id"]
        return created["id"]

    def add_tag_to_assets(self, tag_id: str, asset_ids: list[str]) -> None:
        self._request("PUT", f"/api/tags/{tag_id}/assets", {"ids": asset_ids})


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    asset_id      TEXT PRIMARY KEY,
    updated_at    TEXT,
    fingerprint   TEXT,
    tags          TEXT,
    scores        TEXT,
    model         TEXT,
    processed_at  TEXT
);
CREATE TABLE IF NOT EXISTS failures (
    asset_id   TEXT PRIMARY KEY,
    updated_at TEXT,
    error      TEXT,
    attempts   INTEGER DEFAULT 0,
    last_at    TEXT
);
"""


class State:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.executescript(SCHEMA)
        cols = {row[1] for row in self.db.execute("PRAGMA table_info(processed)")}
        if "fingerprint" not in cols:
            self.db.execute("ALTER TABLE processed ADD COLUMN fingerprint TEXT")
        self.db.commit()

    def seen(self, asset_id: str) -> str | None:
        row = self.db.execute(
            "SELECT fingerprint FROM processed WHERE asset_id=?", (asset_id,)
        ).fetchone()
        return row[0] if row else None

    def record(self, asset_id: str, updated_at: str, fingerprint: str, tags: list[str],
               scores: dict, model: str) -> None:
        self.db.execute(
            "INSERT INTO processed(asset_id, updated_at, fingerprint, tags, scores, model, processed_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(asset_id) DO UPDATE SET "
            "updated_at=excluded.updated_at, fingerprint=excluded.fingerprint, "
            "tags=excluded.tags, scores=excluded.scores, "
            "model=excluded.model, processed_at=excluded.processed_at",
            (asset_id, updated_at, fingerprint, json.dumps(tags), json.dumps(scores), model, now()),
        )
        self.db.execute("DELETE FROM failures WHERE asset_id=?", (asset_id,))
        self.db.commit()

    def fail(self, asset_id: str, updated_at: str, error: str) -> None:
        self.db.execute(
            "INSERT INTO failures(asset_id, updated_at, error, attempts, last_at) "
            "VALUES(?,?,?,1,?) ON CONFLICT(asset_id) DO UPDATE SET "
            "error=excluded.error, attempts=failures.attempts+1, last_at=excluded.last_at",
            (asset_id, updated_at, error[:500], now()),
        )
        self.db.commit()

    def totals(self) -> dict:
        done = self.db.execute("SELECT COUNT(*) FROM processed").fetchone()[0]
        failed = self.db.execute("SELECT COUNT(*) FROM failures").fetchone()[0]
        return {"processed": done, "failed": failed}

    def recent_failures(self, limit: int = 20) -> list[tuple]:
        return list(self.db.execute(
            "SELECT asset_id, attempts, error, last_at FROM failures "
            "ORDER BY last_at DESC LIMIT ?", (limit,)))


def fingerprint(asset: dict) -> str:
    """Identity of the *pixels*, not the row.

    Immich bumps `updatedAt` every time tags change, so that field is useless
    as a dedupe key — writing our own tags would make every asset look new
    forever. The checksum is the file's content hash: it moves only when the
    photo itself is re-uploaded or edited.
    """
    return asset.get("checksum") or asset.get("fileModifiedAt") or asset.get("updatedAt", "")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Eye runner
# ---------------------------------------------------------------------------


class Eye:
    def __init__(self, binary: str, timeout: float = 300.0):
        self.binary = binary
        self.timeout = timeout

    def analyze(self, files: list[str]) -> list[dict]:
        """Run the CLI over a batch. Returns one result dict per file."""
        cmd = [self.binary, "eye", *files, "--json"]
        env = dict(os.environ)
        env.setdefault("DESERTANT_NO_COLOR", "1")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout, env=env)
        if proc.returncode != 0:
            raise RuntimeError(f"desertant exited {proc.returncode}: {proc.stderr.strip()[:400]}")
        out = proc.stdout.strip()
        # The CLI prints one JSON array for a batch, but it has also printed
        # concatenated objects; decode whatever is there rather than assume.
        decoder = json.JSONDecoder()
        payload: list[dict] = []
        pos = 0
        while pos < len(out):
            while pos < len(out) and out[pos] in " \n\r\t":
                pos += 1
            if pos >= len(out):
                break
            value, pos = decoder.raw_decode(out, pos)
            if isinstance(value, list):
                payload.extend(value)
            else:
                payload.append(value)
        if len(payload) != len(files):
            raise RuntimeError(f"desertant returned {len(payload)} results for {len(files)} files")
        for item, path in zip(payload, files):
            item.setdefault("input", path)
        return payload


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


@dataclass
class Config:
    immich_url: str
    api_key: str
    desertant: str = "desertant"
    state_db: str = "/var/lib/immich-eye/state.db"
    prefix: str = "eye"
    batch: int = 24
    page_size: int = 200
    limit: int | None = None
    dry_run: bool = False
    force: bool = False
    since: str | None = None
    max_scene_tags: int = 8
    include_scenes: bool = True
    max_pages: int | None = None
    kuma_push_url: str | None = None
    log: object = field(default=sys.stdout)


def log(msg: str) -> None:
    print(f"{now()} {msg}", flush=True)


def kuma_ping(url: str | None, ok: bool, msg: str) -> None:
    if not url:
        return
    try:
        sep = "&" if "?" in url else "?"
        query = urllib.parse.urlencode({"status": "up" if ok else "down", "msg": msg[:200]})
        urllib.request.urlopen(f"{url}{sep}{query}", timeout=15).read()
    except Exception as exc:  # monitoring must never fail the run
        log(f"kuma push failed: {exc}")


def run(cfg: Config) -> int:
    immich = Immich(cfg.immich_url, cfg.api_key)
    eye = Eye(cfg.desertant)
    state = State(cfg.state_db)
    policy = TagPolicy(prefix=cfg.prefix, max_scene_tags=cfg.max_scene_tags,
                       include_scenes=cfg.include_scenes)

    try:
        ver = immich.version()
        log(f"immich {ver} at {cfg.immich_url}; prefix '{cfg.prefix}'; dry_run={cfg.dry_run}")
    except Exception as exc:
        log(f"cannot reach Immich: {exc}")
        kuma_ping(cfg.kuma_push_url, False, f"immich unreachable: {exc}")
        return 2

    page = 1
    examined = 0
    processed = 0
    skipped = 0
    failed = 0
    tag_counts: dict[str, int] = {}

    tmpdir = Path(tempfile.mkdtemp(prefix="immich-eye-"))
    try:
        while True:
            if cfg.max_pages and page > cfg.max_pages:
                break
            assets = immich.search_images(page, cfg.page_size)
            items = assets.get("items") or []
            if not items:
                break

            todo = []
            for asset in items:
                if asset.get("isTrashed"):
                    continue
                if cfg.since and asset.get("fileCreatedAt", "") < cfg.since:
                    continue
                seen = None if cfg.force else state.seen(asset["id"])
                if seen and seen == fingerprint(asset):
                    skipped += 1
                    continue
                todo.append(asset)
            examined += len(items)

            if cfg.limit is not None:
                todo = todo[: max(0, cfg.limit - processed)]

            for offset in range(0, len(todo), cfg.batch):
                chunk = todo[offset: offset + cfg.batch]
                files, by_path = [], {}
                for asset in chunk:
                    path = tmpdir / f"{asset['id']}.jpg"
                    try:
                        path.write_bytes(immich.preview(asset["id"]))
                    except Exception as exc:
                        state.fail(asset["id"], asset.get("updatedAt", ""), f"preview: {exc}")
                        failed += 1
                        continue
                    files.append(str(path))
                    by_path[str(path)] = asset

                if not files:
                    continue
                try:
                    results = eye.analyze(files)
                except Exception as exc:
                    log(f"batch failed ({len(files)} files): {exc}")
                    for path in files:
                        asset = by_path[path]
                        state.fail(asset["id"], asset.get("updatedAt", ""), str(exc))
                        failed += 1
                    continue

                writes: dict[str, list[str]] = {}
                for result in results:
                    asset = by_path.get(result.get("input", ""))
                    if not asset:
                        continue
                    tags = policy.select(result)
                    scores = result.get("scores") or {}
                    for tag in tags:
                        writes.setdefault(tag, []).append(asset["id"])
                        tag_counts[tag] = tag_counts.get(tag, 0) + 1
                    if cfg.dry_run:
                        processed += 1
                        continue
                    try:
                        state.record(asset["id"], asset.get("updatedAt", ""), fingerprint(asset),
                                     tags, scores, result.get("model", "eye"))
                        processed += 1
                    except Exception as exc:
                        state.fail(asset["id"], asset.get("updatedAt", ""), f"state: {exc}")
                        failed += 1

                for tag, ids in writes.items():
                    if cfg.dry_run:
                        continue
                    try:
                        tag_id = immich.ensure_tag(tag)
                        immich.add_tag_to_assets(tag_id, ids)
                    except Exception as exc:
                        log(f"tag write failed for {tag} ({len(ids)} assets): {exc}")
                        failed += len(ids)

                for path in files:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
                log(f"page {page} batch {offset // cfg.batch + 1}: "
                    f"{len(results)} analysed, {len(writes)} distinct tags")

                if cfg.limit is not None and processed >= cfg.limit:
                    break

            if cfg.limit is not None and processed >= cfg.limit:
                break
            if not assets.get("nextPage"):
                break
            page += 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    summary = (f"examined={examined} processed={processed} skipped={skipped} "
               f"failed={failed} dry_run={cfg.dry_run}")
    log(f"done: {summary}")
    top = sorted(tag_counts.items(), key=lambda kv: -kv[1])[:12]
    if top:
        log("top tags: " + ", ".join(f"{t}({n})" for t, n in top))
    log(f"state: {state.totals()}")
    kuma_ping(cfg.kuma_push_url, failed == 0,
              summary + ("" if failed == 0 else " — see failures table"))
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def env_str(name: str, default: str) -> str:
    return os.environ.get(name) or default


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


def load_api_key() -> str | None:
    direct = env("IMMICH_API_KEY")
    if direct:
        return direct.strip()
    key_file = env("IMMICH_API_KEY_FILE")
    if key_file and Path(key_file).exists():
        return Path(key_file).read_text().strip()
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Eye → Immich sidecar")
    ap.add_argument("--once", action="store_true", help="run one pass (default)")
    ap.add_argument("--dry-run", action="store_true", help="analyse but write nothing")
    ap.add_argument("--force", action="store_true", help="re-process unchanged assets")
    ap.add_argument("--limit", type=int, help="stop after N processed assets")
    ap.add_argument("--since", help="only assets created after this ISO date")
    ap.add_argument("--batch", type=int, help="files per desertant invocation")
    ap.add_argument("--page-size", type=int, help="assets per Immich search page")
    ap.add_argument("--max-pages", type=int, help="stop after N pages")
    ap.add_argument("--max-scene-tags", type=int, help="scene tags per asset")
    ap.add_argument("--no-scenes", action="store_true", help="skip scene tags entirely")
    ap.add_argument("--prefix", help="tag namespace (default 'eye')")
    ap.add_argument("--report", action="store_true", help="print state summary and exit")
    args = ap.parse_args(argv)

    api_key = load_api_key()
    if not api_key:
        ap.error("no API key: set IMMICH_API_KEY or IMMICH_API_KEY_FILE")

    cfg = Config(
        immich_url=env_str("IMMICH_URL", "https://immich.chipmunk-bramble.ts.net"),
        api_key=api_key,
        desertant=env_str("DESERTANT_BIN", "desertant"),
        state_db=env_str("STATE_DB", "/var/lib/immich-eye/state.db"),
        prefix=args.prefix or env_str("TAG_PREFIX", "eye"),
        batch=args.batch or env_int("BATCH", 24),
        page_size=args.page_size or env_int("PAGE_SIZE", 200),
        limit=args.limit,
        dry_run=args.dry_run or env("DRY_RUN", "0") == "1",
        force=args.force,
        since=args.since,
        max_scene_tags=args.max_scene_tags if args.max_scene_tags is not None else env_int("MAX_SCENE_TAGS", 8),
        include_scenes=not args.no_scenes,
        max_pages=args.max_pages,
        kuma_push_url=env("KUMA_PUSH_URL"),
    )

    if args.report:
        state = State(cfg.state_db)
        print(json.dumps({"totals": state.totals(),
                          "failures": state.recent_failures()}, indent=2))
        return 0

    return run(cfg)


if __name__ == "__main__":
    sys.exit(main())
