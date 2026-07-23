"""SIDFlow portable similarity export support for the SID Jukebox.

The published full export is slimmed into a private u64deck SQLite database.
Only track identity, diagnostic classifier values and normalised perceptual
feature vectors are retained; the heavyweight source feature JSON is deleted
once the compact database has been promoted successfully.
"""

from __future__ import annotations

import gc
import hashlib
import heapq
import json
import math
import operator
import os
import shutil
import sqlite3
import struct
import threading
import time
import uuid
from array import array
from contextlib import closing, nullcontext
from pathlib import Path
from typing import Callable, Iterable, Mapping

SCHEMA_VERSION = "sidcorr-1"
VECTOR_SCHEMA_VERSION = "u64deck-featvec-1"
FULL_SQLITE = "sidcorr-hvsc-full-sidcorr-1.sqlite"
FULL_MANIFEST = "sidcorr-hvsc-full-sidcorr-1.manifest.json"
MOBILE_SQLITE = "sidcorr-hvsc-mobile-sidcorr-1.sqlite"
MOBILE_MANIFEST = "sidcorr-hvsc-mobile-sidcorr-1.manifest.json"
CHECKSUMS = "SHA256SUMS"
LATEST_RELEASE_API = "https://api.github.com/repos/chrisgleissner/sidflow-data/releases/latest"
LATEST_DOWNLOAD_BASE = "https://github.com/chrisgleissner/sidflow-data/releases/latest/download"

FEATURE_DIMENSIONS = (
    "bpm", "dynamicRange", "energy", "inharmonicity", "lowFrequencyEnergyRatio",
    "mfccMean1", "mfccMean2", "mfccMean3", "mfccMean4", "mfccMean5",
    "mfccStd1", "mfccStd2", "mfccStd3", "mfccStd4", "mfccStd5",
    "onsetDensity", "pitchSalience", "rhythmicRegularity", "rms",
    "spectralCentroid", "spectralCentroidStd", "spectralContrastMean",
    "spectralContrastStd", "spectralCrest", "spectralEntropy",
    "spectralFlatnessDb", "spectralFluxMean", "spectralRolloff",
    "zeroCrossingRate", "sidAdsrPadRatio", "sidAdsrPluckRatio",
    "sidArpeggioActivity", "sidFilterCutoffMean", "sidFilterMotion",
    "sidGateOnsetDensity", "sidMelodicClarity", "sidPwmActivity",
    "sidRhythmicRegularity", "sidRoleAccompanimentRatio", "sidRoleBassRatio",
    "sidRoleLeadRatio", "sidSyncopation", "sidVoiceRoleEntropy",
    "sidWaveMixedRatio", "sidWaveNoiseRatio", "sidWavePulseRatio",
    "sidWaveSawRatio", "sidWaveTriangleRatio",
)
FEATURE_COUNT = len(FEATURE_DIMENSIONS)
_VECTOR_STRUCT = struct.Struct("<" + "f" * FEATURE_COUNT)
Progress = Callable[[str, int, int], None]


def validate_manifest(manifest: Mapping) -> dict:
    """Validate and normalise the public manifest contract."""
    if not isinstance(manifest, Mapping):
        raise ValueError("SIDFlow manifest is not a JSON object")
    found = str(manifest.get("schema_version") or "")
    if found != SCHEMA_VERSION:
        raise ValueError(
            f"SIDFlow schema {found or 'unknown'} is not supported; expected {SCHEMA_VERSION}"
        )
    track_count = int(manifest.get("track_count") or 0)
    if track_count < 1:
        raise ValueError("SIDFlow manifest has no tracks")
    out = dict(manifest)
    out["schema_version"] = found
    out["track_count"] = track_count
    out["vector_dimensions"] = int(manifest.get("vector_dimensions") or 0)
    out["neighbor_row_count"] = int(manifest.get("neighbor_row_count") or 0)
    return out


def parse_sha256sums(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or len(parts[0]) != 64:
            continue
        name = parts[1].strip().lstrip("*")
        while name.startswith("./"):
            name = name[2:]
        out[name] = parts[0].lower()
    return out


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def normalise_hvsc_relative(device_path: str, hvsc_root: str) -> str | None:
    """Return an HVSC-relative path, preserving spelling but matching roots case-insensitively."""
    path = "/" + "/".join(
        part for part in str(device_path or "").replace("\\", "/").split("/")
        if part and part != "."
    )
    root = "/" + "/".join(
        part for part in str(hvsc_root or "").replace("\\", "/").split("/")
        if part and part != "."
    )
    if root == "/" or not hvsc_root:
        return None
    pkey, rkey = path.casefold(), root.casefold()
    if pkey == rkey:
        return ""
    prefix = rkey.rstrip("/") + "/"
    if not pkey.startswith(prefix):
        return None
    relative = path[len(root):].lstrip("/")
    # HVSC archives commonly contain a C64Music top-level directory even when
    # the configured root points one level above it.
    if relative.casefold() == "c64music":
        return ""
    if relative.casefold().startswith("c64music/"):
        relative = relative[len("C64Music/"):]
    return relative


def build_track_id(sid_path: str, song_index: int) -> str:
    rel = str(sid_path or "").replace("\\", "/").lstrip("/")
    return f"{rel}#{max(1, int(song_index or 1))}"


def _numeric_feature(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    number = float(value)
    return number if math.isfinite(number) else 0.0


def extract_feature_vector(features_json: str | bytes | Mapping | None) -> tuple[float, ...]:
    """Extract the stable 48-dimensional perceptual vector from features_json."""
    if isinstance(features_json, Mapping):
        data = features_json
    else:
        try:
            if isinstance(features_json, bytes):
                features_json = features_json.decode("utf-8")
            data = json.loads(features_json or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            data = {}
    if not isinstance(data, Mapping):
        data = {}
    return tuple(_numeric_feature(data.get(name)) for name in FEATURE_DIMENSIONS)


def normalise_vector(values: Iterable[float], means: Iterable[float],
                     stddevs: Iterable[float]) -> tuple[float, ...]:
    z = [((float(value) - float(mean)) / float(std)) if float(std) > 1e-12 else 0.0
         for value, mean, std in zip(values, means, stddevs)]
    norm = math.sqrt(sum(value * value for value in z))
    if norm <= 1e-12:
        return tuple(0.0 for _ in z)
    return tuple(value / norm for value in z)


def cosine_similarity(seed: Iterable[float], candidate: Iterable[float]) -> float:
    """Cosine similarity helper retained for fixtures and diagnostics."""
    a = tuple(float(value) for value in seed)
    b = tuple(float(value) for value in candidate)
    dot = sum(map(operator.mul, a, b))
    na = math.sqrt(sum(value * value for value in a))
    nb = math.sqrt(sum(value * value for value in b))
    return dot / (na * nb) if na and nb else -1.0


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _write_meta(dst: sqlite3.Connection, manifest: Mapping, source_count: int,
                means: list[float], stddevs: list[float], duplicate_ratio: float) -> None:
    warning = ""
    if duplicate_ratio > 0.5:
        warning = (
            f"SIDFlow feature data appears degenerate: {duplicate_ratio:.1%} of tracks "
            "share an identical extracted vector. Re-download a full feature export."
        )
    rows = [
        ("schema_version", SCHEMA_VERSION),
        ("vector_schema_version", VECTOR_SCHEMA_VERSION),
        ("feature_dimensions_json", json.dumps(FEATURE_DIMENSIONS, separators=(",", ":"))),
        ("feature_means_json", json.dumps(means, separators=(",", ":"))),
        ("feature_stddevs_json", json.dumps(stddevs, separators=(",", ":"))),
        ("duplicate_vector_ratio", f"{duplicate_ratio:.9f}"),
        ("quality_warning", warning),
        ("manifest_json", json.dumps(dict(manifest), sort_keys=True, separators=(",", ":"))),
        ("imported_at", time.strftime("%Y-%m-%dT%H:%M:%S")),
        ("track_count", str(source_count)),
        ("generated_at", str(manifest.get("generated_at") or "")),
        ("export_profile", str(manifest.get("export_profile") or "")),
        ("neighbor_row_count", str(int(manifest.get("neighbor_row_count") or 0))),
    ]
    dst.executemany("INSERT INTO meta(key,value) VALUES(?,?)", rows)


def slim_database(source: Path, destination: Path, manifest: Mapping,
                  progress: Progress | None = None) -> dict:
    """Build the compact local feature-vector database from a full SIDFlow export."""
    source = Path(source)
    destination = Path(destination)
    manifest = validate_manifest(manifest)
    # Each import uses a unique destination path. Never unlink a fixed-name
    # build file here: a stale Windows/AV-held artifact must not block a new run.
    if destination.exists():
        raise FileExistsError(f"SIDFlow build destination already exists: {destination.name}")
    uri = source.resolve().as_uri() + "?mode=ro"
    src = sqlite3.connect(uri, uri=True)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(destination)
    copied_neighbors = 0
    try:
        if src.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("SIDFlow SQLite quick_check failed")
        required = {
            "track_id", "sid_path", "song_index", "e", "m", "c", "p", "features_json"
        }
        columns = _table_columns(src, "tracks")
        missing = required - columns
        if missing:
            raise ValueError(
                "SIDFlow full export is missing required feature data: " + ", ".join(sorted(missing))
            )
        source_count = int(src.execute("SELECT COUNT(*) FROM tracks").fetchone()[0])
        table_names = {
            str(row[0]) for row in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "meta" in table_names:
            try:
                embedded_rows = src.execute("SELECT key,value FROM meta").fetchall()
                embedded = {str(row[0]): str(row[1]) for row in embedded_rows}
                embedded_manifest = json.loads(embedded.get("manifest_json") or "{}")
                if embedded_manifest:
                    embedded_valid = validate_manifest(embedded_manifest)
                    if int(embedded_valid["track_count"]) != int(manifest["track_count"]):
                        raise ValueError("SIDFlow embedded manifest does not match the sidecar manifest")
            except (sqlite3.Error, json.JSONDecodeError) as exc:
                raise ValueError(f"SIDFlow meta table could not be read: {exc}") from exc
        if source_count != int(manifest["track_count"]):
            raise ValueError(
                f"SIDFlow row count {source_count:,} does not match manifest {int(manifest['track_count']):,}"
            )

        dst.executescript("""
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=MEMORY;
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE tracks (
                track_id TEXT PRIMARY KEY,
                sid_path TEXT NOT NULL COLLATE NOCASE,
                sid_path_key TEXT NOT NULL,
                song_index INTEGER NOT NULL,
                e REAL NOT NULL,
                m REAL NOT NULL,
                c REAL NOT NULL,
                p REAL NULL,
                feature_vector BLOB NOT NULL
            ) WITHOUT ROWID;
            CREATE INDEX idx_sidflow_sid_path ON tracks(sid_path_key, song_index);
            CREATE TABLE neighbors (
                profile TEXT NOT NULL,
                seed_track_id TEXT NOT NULL,
                neighbor_track_id TEXT NOT NULL,
                rank INTEGER NOT NULL,
                similarity REAL NOT NULL,
                PRIMARY KEY(profile, seed_track_id, rank)
            ) WITHOUT ROWID;
            CREATE TABLE raw_vector_hashes (hash BLOB NOT NULL);
            CREATE INDEX idx_raw_vector_hash ON raw_vector_hashes(hash);
        """)

        sums = [0.0] * FEATURE_COUNT
        sums_sq = [0.0] * FEATURE_COUNT
        read = src.execute(
            "SELECT track_id,sid_path,song_index,e,m,c,p,features_json FROM tracks ORDER BY track_id"
        )
        copied = 0
        while True:
            rows = read.fetchmany(2000)
            if not rows:
                break
            inserts = []
            hashes = []
            for row in rows:
                vector = extract_feature_vector(row["features_json"])
                for index, value in enumerate(vector):
                    sums[index] += value
                    sums_sq[index] += value * value
                raw_blob = _VECTOR_STRUCT.pack(*vector)
                hashes.append((hashlib.blake2b(raw_blob, digest_size=8).digest(),))
                sid_path = str(row["sid_path"])
                inserts.append((
                    str(row["track_id"]), sid_path, sid_path.casefold(), int(row["song_index"]),
                    float(row["e"]), float(row["m"]), float(row["c"]),
                    None if row["p"] is None else float(row["p"]), raw_blob,
                ))
            dst.executemany(
                "INSERT INTO tracks(track_id,sid_path,sid_path_key,song_index,e,m,c,p,feature_vector) "
                "VALUES(?,?,?,?,?,?,?,?,?)", inserts,
            )
            dst.executemany("INSERT INTO raw_vector_hashes(hash) VALUES(?)", hashes)
            copied += len(rows)
            if progress:
                progress("extracting", copied, source_count)

        means = [value / source_count for value in sums]
        stddevs = [
            math.sqrt(max(0.0, (sums_sq[index] / source_count) - means[index] ** 2))
            for index in range(FEATURE_COUNT)
        ]
        max_identical = int(dst.execute(
            "SELECT COALESCE(MAX(n),0) FROM (SELECT COUNT(*) AS n FROM raw_vector_hashes GROUP BY hash)"
        ).fetchone()[0])
        duplicate_ratio = (max_identical / source_count) if source_count else 1.0

        normalise_read = dst.execute("SELECT track_id,feature_vector FROM tracks ORDER BY track_id")
        normalised = 0
        while True:
            rows = normalise_read.fetchmany(2000)
            if not rows:
                break
            updates = []
            for track_id, raw_blob in rows:
                raw = _VECTOR_STRUCT.unpack(raw_blob)
                unit = normalise_vector(raw, means, stddevs)
                updates.append((_VECTOR_STRUCT.pack(*unit), str(track_id)))
            dst.executemany("UPDATE tracks SET feature_vector=? WHERE track_id=?", updates)
            normalised += len(rows)
            if progress:
                progress("normalising", normalised, source_count)

        requested_neighbors = int(manifest.get("neighbor_row_count") or 0)
        if requested_neighbors:
            required_neighbor = {"profile", "seed_track_id", "neighbor_track_id", "rank", "similarity"}
            neighbor_columns = _table_columns(src, "neighbors")
            missing_neighbor = required_neighbor - neighbor_columns
            if missing_neighbor:
                raise ValueError(
                    "SIDFlow neighbors table is missing: " + ", ".join(sorted(missing_neighbor))
                )
            source_neighbors = int(src.execute("SELECT COUNT(*) FROM neighbors").fetchone()[0])
            if source_neighbors != requested_neighbors:
                raise ValueError(
                    f"SIDFlow neighbor row count {source_neighbors:,} does not match manifest {requested_neighbors:,}"
                )
            nread = src.execute(
                "SELECT profile,seed_track_id,neighbor_track_id,rank,similarity "
                "FROM neighbors ORDER BY profile,seed_track_id,rank"
            )
            while True:
                rows = nread.fetchmany(5000)
                if not rows:
                    break
                dst.executemany(
                    "INSERT INTO neighbors(profile,seed_track_id,neighbor_track_id,rank,similarity) "
                    "VALUES(?,?,?,?,?)", [tuple(row) for row in rows],
                )
                copied_neighbors += len(rows)
                if progress:
                    progress("neighbors", copied_neighbors, requested_neighbors)

        dst.execute("DROP TABLE raw_vector_hashes")
        _write_meta(dst, manifest, source_count, means, stddevs, duplicate_ratio)
        dst.commit()
        dst.execute("VACUUM")
        dst.commit()
    except Exception:
        try:
            dst.close()
        finally:
            src.close()
        try:
            destination.unlink()
        except OSError:
            pass
        raise
    finally:
        try:
            src.close()
        except Exception:
            pass
        try:
            dst.close()
        except Exception:
            pass
    gc.collect()
    return {
        "tracks": source_count,
        "neighbors": copied_neighbors,
        "bytes": destination.stat().st_size,
        "duplicate_vector_ratio": duplicate_ratio,
        "vector_schema_version": VECTOR_SCHEMA_VERSION,
    }


class SimilarityStore:
    """Read/query wrapper around u64deck's compact SIDFlow database."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.file_lock = threading.RLock()
        self._signature: tuple[int, int] | None = None
        self._vectors: list[tuple[str, str, str, str, int, float, float, float, float | None, array]] = []
        self._by_track: dict[str, tuple[str, str, str, str, int, float, float, float, float | None, array]] = {}
        self._quality_warning = ""

    def invalidate(self) -> None:
        with self.file_lock:
            self._signature = None
            self._vectors = []
            self._by_track = {}
            self._quality_warning = ""

    def _connect(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def status(self) -> dict:
        if not self.path.is_file():
            return {"available": False, "schema_version": "", "vector_schema_version": "",
                    "tracks": 0, "neighbors": 0, "bytes": 0}
        try:
            with self.file_lock:
                with closing(self._connect()) as conn:
                    meta = {row["key"]: row["value"] for row in conn.execute("SELECT key,value FROM meta")}
                    tracks = int(conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0])
                    neighbors = int(conn.execute("SELECT COUNT(*) FROM neighbors").fetchone()[0])
            manifest = json.loads(meta.get("manifest_json") or "{}")
            vector_schema = meta.get("vector_schema_version", "")
            schema_ok = meta.get("schema_version") == SCHEMA_VERSION
            vector_ok = vector_schema == VECTOR_SCHEMA_VERSION
            warning = meta.get("quality_warning", "")
            error = ""
            if schema_ok and not vector_ok:
                error = "SIDFlow similarity data uses an older local vector format; re-download it"
            return {
                "available": bool(schema_ok and vector_ok),
                "schema_version": meta.get("schema_version", ""),
                "vector_schema_version": vector_schema,
                "tracks": tracks,
                "neighbors": neighbors,
                "bytes": self.path.stat().st_size,
                "generated_at": meta.get("generated_at", ""),
                "imported_at": meta.get("imported_at", ""),
                "export_profile": meta.get("export_profile", ""),
                "release_tag": str(manifest.get("u64deck_release_tag") or ""),
                "source_asset": str(manifest.get("u64deck_source_asset") or ""),
                "duplicate_vector_ratio": float(meta.get("duplicate_vector_ratio") or 0.0),
                "quality_warning": warning,
                "error": error,
                "manifest": manifest,
            }
        except Exception as exc:
            return {"available": False, "schema_version": "", "vector_schema_version": "",
                    "tracks": 0, "neighbors": 0, "bytes": self.path.stat().st_size,
                    "error": str(exc)}

    def _ensure_vectors(self) -> None:
        with self.file_lock:
            stat = self.path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            if signature == self._signature and self._vectors:
                return
            with closing(self._connect()) as conn:
                meta = {row["key"]: row["value"] for row in conn.execute("SELECT key,value FROM meta")}
                if meta.get("vector_schema_version") != VECTOR_SCHEMA_VERSION:
                    raise ValueError("SIDFlow local vector format is obsolete; re-download similarity data")
                rows = conn.execute(
                    "SELECT track_id,sid_path,song_index,e,m,c,p,feature_vector "
                    "FROM tracks ORDER BY track_id COLLATE NOCASE"
                ).fetchall()
            vectors = []
            for row in rows:
                blob = bytes(row["feature_vector"])
                if len(blob) != _VECTOR_STRUCT.size:
                    raise ValueError("SIDFlow local feature vector has an invalid size")
                vector = array("f")
                vector.frombytes(blob)
                track_id = str(row["track_id"])
                sid_path = str(row["sid_path"])
                vectors.append((
                    track_id, track_id.casefold(), sid_path, sid_path.casefold(),
                    int(row["song_index"]), float(row["e"]), float(row["m"]),
                    float(row["c"]), None if row["p"] is None else float(row["p"]), vector,
                ))
            self._vectors = vectors
            self._by_track = {row[1]: row for row in vectors}
            self._quality_warning = meta.get("quality_warning", "")
            self._signature = signature

    def warm(self) -> int:
        self._ensure_vectors()
        return len(self._vectors)

    def lookup(self, sid_path: str, song_index: int) -> dict | None:
        self._ensure_vectors()
        row = self._by_track.get(build_track_id(sid_path, song_index).casefold())
        return self._row_dict(row, 1.0) if row else None

    @staticmethod
    def _row_dict(row, similarity: float) -> dict:
        return {
            "track_id": row[0], "sid_path": row[2], "song_index": row[4],
            "e": row[5], "m": row[6], "c": row[7], "p": row[8],
            "similarity": float(similarity),
        }

    def rank(self, seed_track_id: str, *, limit: int = 20,
             present_paths: set[str] | None = None,
             exclude_track_ids: Iterable[str] = ()) -> list[dict]:
        self._ensure_vectors()
        if self._quality_warning:
            raise ValueError(self._quality_warning)
        seed = self._by_track.get(str(seed_track_id).casefold())
        if not seed:
            raise KeyError(seed_track_id)
        excluded = {str(value).casefold() for value in exclude_track_ids}
        excluded.add(str(seed_track_id).casefold())
        present = {str(value).casefold() for value in present_paths} if present_paths is not None else None

        with self.file_lock:
            with closing(self._connect()) as conn:
                has_neighbors = int(conn.execute("SELECT COUNT(*) FROM neighbors").fetchone()[0]) > 0
                if has_neighbors:
                    rows = conn.execute(
                        "SELECT t.track_id,t.sid_path,t.song_index,t.e,t.m,t.c,t.p,n.similarity "
                        "FROM neighbors n JOIN tracks t ON t.track_id=n.neighbor_track_id "
                        "WHERE n.seed_track_id=? ORDER BY n.profile,n.rank LIMIT ?",
                        (seed[0], max(200, int(limit) * 20)),
                    ).fetchall()
                    out = []
                    seen = set()
                    for row in rows:
                        key = str(row["track_id"]).casefold()
                        if key in seen or key in excluded:
                            continue
                        if present is not None and str(row["sid_path"]).casefold() not in present:
                            continue
                        seen.add(key)
                        track_id = str(row["track_id"]); sid_path = str(row["sid_path"])
                        compact = (
                            track_id, track_id.casefold(), sid_path, sid_path.casefold(),
                            row["song_index"], row["e"], row["m"], row["c"], row["p"], array("f"),
                        )
                        out.append(self._row_dict(compact, row["similarity"]))
                        if len(out) >= limit:
                            return out

        wanted = max(1, int(limit))
        best: list[tuple[float, int, tuple]] = []
        seed_vector = seed[9]
        for sequence, row in enumerate(self._vectors):
            if row[1] in excluded:
                continue
            if present is not None and row[3] not in present:
                continue
            score = float(sum(map(operator.mul, seed_vector, row[9])))
            entry = (score, -sequence, row)
            if len(best) < wanted:
                heapq.heappush(best, entry)
            elif entry[:2] > best[0][:2]:
                heapq.heapreplace(best, entry)
        best.sort(key=lambda item: (-item[0], -item[1]))
        return [self._row_dict(row, score) for score, _sequence, row in best]


def _replace_with_retries(source: Path, destination: Path, attempts: int) -> None:
    last_error: OSError | None = None
    for attempt in range(max(1, int(attempts))):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            if not isinstance(exc, PermissionError) and getattr(exc, "winerror", None) not in (5, 32):
                raise
            last_error = exc
            gc.collect()
            if attempt + 1 >= max(1, int(attempts)):
                break
            time.sleep(min(1.0, 0.10 * (attempt + 1)))
    if last_error is not None:
        raise last_error


def _validate_compact_database(path: Path) -> None:
    # sqlite3.Connection.__exit__ commits/rolls back but does not close. Use
    # closing explicitly so the subsequent Windows promotion never fights our
    # own open handle to the ready/build database.
    with closing(sqlite3.connect(path)) as conn:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Compact SIDFlow database failed quick_check before promotion")
        meta = {row[0]: row[1] for row in conn.execute("SELECT key,value FROM meta")}
        if meta.get("vector_schema_version") != VECTOR_SCHEMA_VERSION:
            raise ValueError("Compact SIDFlow database has the wrong local vector format")


def _sqlite_backup_promote(source: Path, destination: Path) -> None:
    """Populate the live database without renaming a freshly scanned file.

    Some Windows security products keep both completed build and ready-copy
    files locked long enough to defeat every rename retry. SQLite's online
    backup API reads the validated source and writes the destination through
    SQLite itself, avoiding a filesystem move while the SimilarityStore lock
    prevents u64deck readers from opening the live database mid-copy.
    """
    source = Path(source).resolve()
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    uri = source.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30.0)) as src:
        with closing(sqlite3.connect(destination, timeout=30.0)) as dst:
            src.backup(dst, pages=1024, sleep=0.05)
            dst.commit()
    _validate_compact_database(destination)


def atomic_replace_database(build_path: Path, live_path: Path, *, attempts: int = 30) -> None:
    """Promote a completed database robustly on Windows.

    A direct rename is attempted first. If Windows or antivirus software keeps
    the just-closed SQLite build file locked, copy it to a fresh ready file and
    retry. If both filesystem promotions remain blocked, use SQLite's own backup
    API to populate the live database without renaming either temporary file.
    """
    build_path = Path(build_path)
    live_path = Path(live_path)
    gc.collect()
    try:
        _replace_with_retries(build_path, live_path, attempts)
        return
    except OSError as first_error:
        if not isinstance(first_error, PermissionError) and getattr(first_error, "winerror", None) not in (5, 32):
            raise
        ready = live_path.with_name(f"{live_path.name}.ready-{uuid.uuid4().hex[:8]}")
        try:
            with build_path.open("rb") as src, ready.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            _validate_compact_database(ready)
            _replace_with_retries(ready, live_path, attempts)
            return
        except Exception as ready_error:
            try:
                _sqlite_backup_promote(build_path, live_path)
                return
            except Exception as backup_error:
                raise PermissionError(
                    "Windows could not promote the completed SIDFlow database "
                    f"after rename retries ({ready_error}) or SQLite backup ({backup_error})"
                ) from first_error
        finally:
            try:
                ready.unlink()
            except OSError:
                pass



def _unlink_with_retries(path: Path, attempts: int = 20) -> str:
    """Delete a completed source download without turning promotion into failure."""
    last_error = None
    for attempt in range(max(1, int(attempts))):
        try:
            Path(path).unlink()
            return ""
        except FileNotFoundError:
            return ""
        except OSError as exc:
            last_error = exc
            gc.collect()
            if attempt + 1 < attempts:
                time.sleep(min(1.0, 0.10 * (attempt + 1)))
    return str(last_error or "could not delete source download")

def slim_and_promote(source: Path, live_path: Path, manifest: Mapping,
                     progress: Progress | None = None,
                     promotion_lock=None) -> dict:
    """Slim a downloaded export, atomically promote it and delete the source."""
    source = Path(source)
    live_path = Path(live_path)
    build_path = live_path.with_name(
        f"{live_path.name}.building-{uuid.uuid4().hex[:8]}"
    )
    try:
        result = slim_database(source, build_path, manifest, progress)
        guard = promotion_lock if promotion_lock is not None else nullcontext()
        with guard:
            atomic_replace_database(build_path, live_path)
        delete_warning = _unlink_with_retries(source)
        if delete_warning:
            result["source_delete_warning"] = delete_warning
        return result
    finally:
        try:
            build_path.unlink()
        except OSError:
            pass
