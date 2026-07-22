"""Stable SQLite index migration for u64deck.

Older releases derived the index filename from the Ultimate's IP address. DHCP
therefore created a new partial database whenever the address changed. This
module consolidates those per-IP databases into one installation-local index
without modifying the originals.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
import time
import uuid
from pathlib import Path
from typing import Callable, Iterable

from index_store import IndexStore

STABLE_INDEX_NAME = ".u64deck-index.sqlite3"
LEGACY_GLOB = ".u64deck-index-*.sqlite3"
MIGRATION_KEY = "stable_index_migration"


def _log_call(log: Callable[[str], None] | None, message: str) -> None:
    if log:
        log(message)


def _fingerprint(path: Path) -> dict:
    stat = path.stat()
    return {
        "name": path.name,
        "bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _read_migration_metadata(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as conn:
            row = conn.execute(
                "SELECT value FROM metadata WHERE key=?", (MIGRATION_KEY,)
            ).fetchone()
        return json.loads(row[0]) if row and row[0] else {}
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return {}


def _snapshot_database(source: Path, target: Path) -> None:
    """Create a transactionally consistent SQLite backup, including WAL data."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    with closing(sqlite3.connect(str(source), timeout=30.0)) as src:
        src.execute("PRAGMA busy_timeout=30000")
        with closing(sqlite3.connect(str(target), timeout=30.0)) as dst:
            src.backup(dst, pages=2048, sleep=0.02)
            dst.commit()


def _table_exists(conn: sqlite3.Connection, schema: str, table: str) -> bool:
    return conn.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _format_fk_violations(rows: Iterable[sqlite3.Row | tuple]) -> str:
    """Return readable foreign-key failure details instead of Row reprs."""
    parts: list[str] = []
    for row in rows:
        values = tuple(row)
        table = str(values[0]) if len(values) > 0 else "?"
        rowid = str(values[1]) if len(values) > 1 else "?"
        parent = str(values[2]) if len(values) > 2 else "?"
        fk_id = str(values[3]) if len(values) > 3 else "?"
        parts.append(f"{table} rowid={rowid} -> {parent} (fk {fk_id})")
    return "; ".join(parts)


def _repair_orphan_rows(conn: sqlite3.Connection) -> dict[str, int]:
    """Remove unusable child rows whose parent catalogue record is absent.

    Older per-IP indexes can contain these after interrupted image-cache
    replacement operations or releases that did not enforce foreign keys.
    The child records cannot be queried safely without their parent and are
    therefore skipped during consolidation rather than aborting the whole
    migration.
    """
    repaired = {"fs_entries": 0, "image_entries": 0}
    if _table_exists(conn, "main", "fs_entries") and _table_exists(conn, "main", "directories"):
        repaired["fs_entries"] = int(conn.execute(
            "SELECT COUNT(*) FROM main.fs_entries AS f "
            "WHERE NOT EXISTS (SELECT 1 FROM main.directories AS d WHERE d.path=f.parent)"
        ).fetchone()[0])
        if repaired["fs_entries"]:
            conn.execute(
                "DELETE FROM main.fs_entries "
                "WHERE NOT EXISTS (SELECT 1 FROM main.directories AS d "
                "WHERE d.path=main.fs_entries.parent)"
            )
    if _table_exists(conn, "main", "image_entries") and _table_exists(conn, "main", "image_cache"):
        repaired["image_entries"] = int(conn.execute(
            "SELECT COUNT(*) FROM main.image_entries AS e "
            "WHERE NOT EXISTS (SELECT 1 FROM main.image_cache AS c "
            "WHERE c.path=e.image_path AND c.size=e.image_size AND c.mtime=e.image_mtime)"
        ).fetchone()[0])
        if repaired["image_entries"]:
            conn.execute(
                "DELETE FROM main.image_entries "
                "WHERE NOT EXISTS (SELECT 1 FROM main.image_cache AS c "
                "WHERE c.path=main.image_entries.image_path "
                "AND c.size=main.image_entries.image_size "
                "AND c.mtime=main.image_entries.image_mtime)"
            )
    return repaired


def _merge_source(
    store: IndexStore,
    source: Path,
    *,
    log: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Merge one upgraded source snapshot into *store*.

    Directory listings and image catalogues are selected as coherent units:
    the newest scan for a path replaces the older path's rows. SID metadata is
    merged per tune. Completed-root/import records use their completion time.
    """
    conn = store._conn  # Internal maintenance operation under the store lock.
    alias = "legacy_src"
    with store._lock:
        conn.commit()
        conn.execute(f"ATTACH DATABASE ? AS {alias}", (str(source),))
        try:
            conn.execute("PRAGMA foreign_keys=OFF")

            # Keep useful non-transient metadata from the newest source. The
            # schema/migration markers are always owned by the destination.
            if _table_exists(conn, alias, "metadata"):
                conn.execute(
                    f"INSERT OR REPLACE INTO main.metadata(key,value) "
                    f"SELECT key,value FROM {alias}.metadata "
                    "WHERE key NOT IN ('schema_version','local_scan_active',"
                    "'sid_scan_active',?)",
                    (MIGRATION_KEY,),
                )

            if _table_exists(conn, alias, "directories") and _table_exists(conn, alias, "fs_entries"):
                conn.executescript(
                    f"""
                    DROP TABLE IF EXISTS temp.changed_dirs;
                    CREATE TEMP TABLE changed_dirs(path TEXT PRIMARY KEY);
                    INSERT INTO changed_dirs(path)
                    SELECT s.path
                    FROM {alias}.directories AS s
                    LEFT JOIN main.directories AS d ON d.path=s.path
                    WHERE d.path IS NULL OR s.scanned_at >= d.scanned_at;

                    DELETE FROM main.fs_entries
                    WHERE parent IN (SELECT path FROM changed_dirs);

                    INSERT INTO main.directories(path,scanned_at)
                    SELECT s.path,s.scanned_at
                    FROM {alias}.directories AS s
                    JOIN changed_dirs AS c ON c.path=s.path
                    WHERE 1
                    ON CONFLICT(path) DO UPDATE SET scanned_at=excluded.scanned_at;

                    INSERT OR REPLACE INTO main.fs_entries
                        (parent,path,name,name_lower,is_dir,size,mtime)
                    SELECT s.parent,s.path,s.name,s.name_lower,s.is_dir,s.size,s.mtime
                    FROM {alias}.fs_entries AS s
                    JOIN changed_dirs AS c ON c.path=s.parent;
                    DROP TABLE temp.changed_dirs;
                    """
                )

            if _table_exists(conn, alias, "image_cache") and _table_exists(conn, alias, "image_entries"):
                conn.executescript(
                    f"""
                    DROP TABLE IF EXISTS temp.changed_images;
                    CREATE TEMP TABLE changed_images(path TEXT PRIMARY KEY);
                    INSERT INTO changed_images(path)
                    SELECT s.path
                    FROM {alias}.image_cache AS s
                    LEFT JOIN (
                        SELECT path,MAX(scanned_at) AS newest
                        FROM main.image_cache GROUP BY path
                    ) AS d ON d.path=s.path
                    GROUP BY s.path
                    HAVING d.path IS NULL OR MAX(s.scanned_at) >= d.newest;

                    -- Foreign keys are disabled while attached sources are
                    -- merged, so delete child catalogue rows explicitly before
                    -- replacing the parent cache records for a path.
                    DELETE FROM main.image_entries
                    WHERE image_path IN (SELECT path FROM changed_images);
                    DELETE FROM main.image_cache
                    WHERE path IN (SELECT path FROM changed_images);

                    INSERT INTO main.image_cache
                        (path,size,mtime,scanned_at,last_access,parse_ok,parse_error)
                    SELECT s.path,s.size,s.mtime,s.scanned_at,s.last_access,
                           s.parse_ok,s.parse_error
                    FROM {alias}.image_cache AS s
                    JOIN changed_images AS c ON c.path=s.path;

                    INSERT OR REPLACE INTO main.image_entries
                        (image_path,image_size,image_mtime,entry_index,name,
                         name_lower,file_type,blocks)
                    SELECT s.image_path,s.image_size,s.image_mtime,s.entry_index,
                           s.name,s.name_lower,s.file_type,s.blocks
                    FROM {alias}.image_entries AS s
                    JOIN {alias}.image_cache AS parent
                      ON parent.path=s.image_path
                     AND parent.size=s.image_size
                     AND parent.mtime=s.image_mtime
                    JOIN changed_images AS c ON c.path=s.image_path;
                    DROP TABLE temp.changed_images;
                    """
                )

            if _table_exists(conn, alias, "sid_metadata"):
                conn.executescript(
                    f"""
                    DROP TABLE IF EXISTS temp.changed_sids;
                    CREATE TEMP TABLE changed_sids(path TEXT PRIMARY KEY COLLATE NOCASE);
                    INSERT INTO changed_sids(path)
                    SELECT s.path
                    FROM {alias}.sid_metadata AS s
                    LEFT JOIN main.sid_metadata AS d
                      ON d.path=s.path COLLATE NOCASE
                    WHERE d.path IS NULL OR s.scanned_at >= d.scanned_at;

                    DELETE FROM main.sid_metadata
                    WHERE path IN (SELECT path FROM changed_sids);

                    INSERT INTO main.sid_metadata
                        (path,parent,name,name_lower,size,mtime,format,version,songs,
                         start_song,title,title_lower,author,author_lower,released,
                         chip,clock,sids,md5,source,scanned_at)
                    SELECT s.path,s.parent,s.name,s.name_lower,s.size,s.mtime,
                           s.format,s.version,s.songs,s.start_song,s.title,
                           s.title_lower,s.author,s.author_lower,s.released,s.chip,
                           s.clock,s.sids,s.md5,s.source,s.scanned_at
                    FROM {alias}.sid_metadata AS s
                    JOIN changed_sids AS c ON c.path=s.path COLLATE NOCASE;
                    DROP TABLE temp.changed_sids;
                    """
                )

            for table, key, stamp, columns in (
                ("index_roots", "root", "completed_at",
                 "root,completed,completed_at,dirs,images,secs"),
                ("local_imports", "root", "completed_at",
                 "root,source_path,volume_id,completed,completed_at,dirs,files,images,errors,secs"),
                ("sid_index_runs", "root", "completed_at",
                 "root,mode,source_path,completed,completed_at,files,parsed,cached,errors,secs"),
            ):
                if not _table_exists(conn, alias, table):
                    continue
                temp_name = "changed_" + table
                conn.executescript(
                    f"""
                    DROP TABLE IF EXISTS temp.{temp_name};
                    CREATE TEMP TABLE {temp_name}(item_key TEXT PRIMARY KEY COLLATE NOCASE);
                    INSERT INTO {temp_name}(item_key)
                    SELECT s.{key}
                    FROM {alias}.{table} AS s
                    LEFT JOIN main.{table} AS d
                      ON d.{key}=s.{key} COLLATE NOCASE
                    WHERE d.{key} IS NULL OR s.{stamp} >= d.{stamp};
                    DELETE FROM main.{table}
                    WHERE {key} IN (SELECT item_key FROM {temp_name});
                    INSERT INTO main.{table}({columns})
                    SELECT {','.join('s.' + c.strip() for c in columns.split(','))}
                    FROM {alias}.{table} AS s
                    JOIN {temp_name} AS c ON c.item_key=s.{key} COLLATE NOCASE;
                    DROP TABLE temp.{temp_name};
                    """
                )

            repaired = _repair_orphan_rows(conn)
            repaired_total = sum(repaired.values())
            if repaired_total:
                details = ", ".join(
                    f"{count} {table}" for table, count in repaired.items() if count
                )
                _log_call(
                    log,
                    f"  index migration: skipped {details} orphan row(s) from {source.name}",
                )

            conn.commit()
            conn.execute("PRAGMA foreign_keys=ON")
            # Catch any relationship damage before accepting the source.
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(
                    "foreign-key validation failed after merging "
                    f"{source.name}: {_format_fk_violations(violations[:8])}"
                )
            return repaired
        except Exception:
            conn.execute("PRAGMA foreign_keys=ON")
            raise
        finally:
            conn.execute(f"DETACH DATABASE {alias}")
            conn.commit()




def _coverage_counts(path: Path) -> dict[str, int]:
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30.0)) as conn:
        def count(sql: str) -> int:
            try:
                return int(conn.execute(sql).fetchone()[0])
            except sqlite3.Error:
                return 0
        return {
            "directories": count("SELECT COUNT(*) FROM directories"),
            "image_paths": count("SELECT COUNT(DISTINCT path) FROM image_cache"),
            "sid_metadata": count("SELECT COUNT(*) FROM sid_metadata"),
        }


def _validate_coverage(destination: Path, sources: Iterable[Path]) -> dict:
    source_counts = [_coverage_counts(path) for path in sources]
    dest = _coverage_counts(destination)
    required = {
        key: max((counts[key] for counts in source_counts), default=0)
        for key in ("directories", "image_paths", "sid_metadata")
    }
    missing = {key: (dest[key], required[key]) for key in required if dest[key] < required[key]}
    if missing:
        raise RuntimeError(f"merged index did not preserve source coverage: {missing}")
    return {"destination": dest, "required_minimum": required}



def _is_windows_sharing_error(exc: OSError) -> bool:
    """Return True for transient Windows access/sharing violations."""
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32}


def _replace_with_retry(source: Path, target: Path, *, attempts: int = 20, delay: float = 0.1) -> None:
    """Atomically replace *target*, tolerating brief AV/indexer file scans."""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            if not _is_windows_sharing_error(exc) or attempt + 1 >= attempts:
                raise
            time.sleep(delay)


def _unlink_with_retry(path: Path, *, attempts: int = 10, delay: float = 0.05) -> None:
    """Delete a migration artefact after all SQLite handles have been closed."""
    for attempt in range(attempts):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if not _is_windows_sharing_error(exc) or attempt + 1 >= attempts:
                raise
            time.sleep(delay)


def _cleanup_database_family(path: Path) -> list[str]:
    """Best-effort removal of a SQLite file and its WAL sidecars."""
    failures: list[str] = []
    for suffix in ("", "-wal", "-shm"):
        candidate = path.with_name(path.name + suffix)
        try:
            _unlink_with_retry(candidate)
        except OSError as exc:
            failures.append(f"{candidate.name}: {exc}")
    return failures

def prepare_stable_index(
    root: Path,
    *,
    log: Callable[[str], None] | None = print,
) -> dict:
    """Return migration status and ensure the stable index exists when needed.

    The original per-IP databases remain untouched. Consistent snapshots are
    stored under ``index-backups/<timestamp>/`` before a temporary destination
    is built and atomically promoted.
    """
    root = Path(root)
    stable = root / STABLE_INDEX_NAME
    legacy = sorted(
        (p for p in root.glob(LEGACY_GLOB) if p.is_file()),
        key=lambda p: p.name.lower(),
    )
    fingerprints = [_fingerprint(path) for path in legacy]
    previous = _read_migration_metadata(stable)
    migrated = {
        (str(item.get("name")), int(item.get("bytes", -1)), int(item.get("mtime_ns", -1)))
        for item in previous.get("sources", []) if isinstance(item, dict)
    }
    current = {(x["name"], x["bytes"], x["mtime_ns"]) for x in fingerprints}

    if stable.is_file() and current.issubset(migrated):
        return {
            "status": "ready",
            "path": stable.name,
            "migrated_sources": len(previous.get("sources", [])),
            "backup_dir": str(previous.get("backup_dir", "")),
            "details": previous,
        }

    if not legacy:
        return {
            "status": "new" if not stable.exists() else "ready",
            "path": stable.name,
            "migrated_sources": int(previous.get("migrated_sources", 0) or 0),
            "backup_dir": str(previous.get("backup_dir", "")),
            "details": previous,
        }

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = root / "index-backups" / f"{stamp}-{uuid.uuid4().hex[:6]}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    snapshots: list[Path] = []
    snapshot_order: dict[Path, int] = {}
    sources = ([stable] if stable.is_file() else []) + legacy

    _log_call(log, f"  index migration: found {len(legacy)} legacy per-IP database(s)")
    for source in sources:
        target = backup_dir / source.name
        _log_call(log, f"  index migration: backing up {source.name}")
        _snapshot_database(source, target)
        # Upgrade only the snapshot. The original file remains byte-for-byte
        # untouched and can be restored manually if ever required.
        upgraded = IndexStore(target)
        upgraded.close()
        snapshots.append(target)
        snapshot_order[target] = int(source.stat().st_mtime_ns)

    temp = root / f"{STABLE_INDEX_NAME}.migrating-{uuid.uuid4().hex[:8]}"
    _cleanup_database_family(temp)
    promoted: Path | None = None

    try:
        destination = IndexStore(temp)
        try:
            # Oldest first; a newer snapshot wins for the same scanned path.
            ordered = sorted(snapshots, key=lambda p: snapshot_order.get(p, 0))
            for number, source in enumerate(ordered, 1):
                _log_call(log, f"  index migration: merging {number}/{len(ordered)} {source.name}")
                _merge_source(destination, source, log=log)

            details = {
                "completed": time.strftime("%Y-%m-%d %H:%M:%S"),
                "sources": fingerprints,
                "migrated_sources": len(legacy),
                "backup_dir": str(backup_dir.relative_to(root)),
            }
            destination.metadata_set(MIGRATION_KEY, json.dumps(details, sort_keys=True))
        finally:
            destination.close()

        validation = _validate_coverage(temp, snapshots)
        details["validation"] = validation
        # Reopen briefly so the final metadata includes validation too.
        finalise = IndexStore(temp)
        finalise.metadata_set(MIGRATION_KEY, json.dumps(details, sort_keys=True))
        finalise.close()

        # Promote through SQLite's backup API so committed WAL pages are
        # folded into one self-contained file before the atomic replacement.
        promoted = root / f"{STABLE_INDEX_NAME}.ready-{uuid.uuid4().hex[:8]}"
        _snapshot_database(temp, promoted)
        _replace_with_retry(promoted, stable)
        cleanup_failures = _cleanup_database_family(temp)
        if cleanup_failures:
            _log_call(log, "  warning: migrated index is ready but temporary cleanup failed: "
                      + "; ".join(cleanup_failures))
        _log_call(log, f"  index migration: complete -> {stable.name}")
        return {
            "status": "migrated",
            "path": stable.name,
            "migrated_sources": len(legacy),
            "backup_dir": str(backup_dir),
            "details": details,
        }
    except Exception as exc:
        cleanup_failures = _cleanup_database_family(temp)
        if promoted is not None:
            cleanup_failures.extend(_cleanup_database_family(promoted))
        _log_call(log, f"  warning: stable index migration failed: {exc}")
        if cleanup_failures:
            _log_call(log, "  warning: migration cleanup also failed: "
                      + "; ".join(cleanup_failures))
        return {
            "status": "failed",
            "path": stable.name,
            "migrated_sources": 0,
            "backup_dir": str(backup_dir),
            "error": str(exc),
        }
