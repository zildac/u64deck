"""SQLite-backed directory and disk-image index for u64deck.

The old JSON caches required rewriting large files and loading the entire
collection into memory. This store updates incrementally, supports concurrent
reader/writer threads through WAL mode, and imports the legacy JSON files once.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable


SCHEMA_VERSION = 4


class IndexStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path,
            timeout=30.0,
            check_same_thread=False,
            isolation_level="DEFERRED",
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _create_schema(self) -> None:
        self._conn.executescript(
            """
            BEGIN;
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS directories (
                path TEXT PRIMARY KEY,
                scanned_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fs_entries (
                parent TEXT NOT NULL,
                path TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                name_lower TEXT NOT NULL,
                is_dir INTEGER NOT NULL,
                size INTEGER NOT NULL DEFAULT 0,
                mtime TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(parent) REFERENCES directories(path) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_fs_parent ON fs_entries(parent);
            CREATE INDEX IF NOT EXISTS idx_fs_name_lower ON fs_entries(name_lower);
            CREATE INDEX IF NOT EXISTS idx_fs_path ON fs_entries(path);

            CREATE TABLE IF NOT EXISTS image_cache (
                path TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime TEXT NOT NULL,
                scanned_at REAL NOT NULL,
                last_access REAL NOT NULL,
                parse_ok INTEGER NOT NULL DEFAULT 1,
                parse_error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(path, size, mtime)
            );
            CREATE INDEX IF NOT EXISTS idx_image_path ON image_cache(path);
            CREATE INDEX IF NOT EXISTS idx_image_access ON image_cache(last_access);

            CREATE TABLE IF NOT EXISTS image_entries (
                image_path TEXT NOT NULL,
                image_size INTEGER NOT NULL,
                image_mtime TEXT NOT NULL,
                entry_index INTEGER NOT NULL,
                name TEXT NOT NULL,
                name_lower TEXT NOT NULL,
                file_type TEXT NOT NULL DEFAULT '',
                blocks INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(image_path, image_size, image_mtime, entry_index),
                FOREIGN KEY(image_path, image_size, image_mtime)
                    REFERENCES image_cache(path, size, mtime) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_image_entries_name
                ON image_entries(name_lower);
            CREATE INDEX IF NOT EXISTS idx_image_entries_path
                ON image_entries(image_path);

            CREATE TABLE IF NOT EXISTS index_roots (
                root TEXT PRIMARY KEY,
                completed TEXT NOT NULL,
                completed_at REAL NOT NULL,
                dirs INTEGER NOT NULL,
                images INTEGER NOT NULL,
                secs REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS local_imports (
                root TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                volume_id TEXT NOT NULL DEFAULT '',
                completed TEXT NOT NULL,
                completed_at REAL NOT NULL,
                dirs INTEGER NOT NULL,
                files INTEGER NOT NULL,
                images INTEGER NOT NULL,
                errors INTEGER NOT NULL DEFAULT 0,
                secs REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scan_seen (
                scan_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                path TEXT NOT NULL,
                PRIMARY KEY(scan_id, kind, path)
            );
            CREATE INDEX IF NOT EXISTS idx_scan_seen
                ON scan_seen(scan_id, kind);

            CREATE TABLE IF NOT EXISTS sid_metadata (
                path TEXT PRIMARY KEY COLLATE NOCASE,
                parent TEXT NOT NULL,
                name TEXT NOT NULL,
                name_lower TEXT NOT NULL,
                size INTEGER NOT NULL DEFAULT 0,
                mtime TEXT NOT NULL DEFAULT '',
                format TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 0,
                songs INTEGER NOT NULL DEFAULT 1,
                start_song INTEGER NOT NULL DEFAULT 1,
                title TEXT NOT NULL DEFAULT '',
                title_lower TEXT NOT NULL DEFAULT '',
                author TEXT NOT NULL DEFAULT '',
                author_lower TEXT NOT NULL DEFAULT '',
                released TEXT NOT NULL DEFAULT '',
                chip TEXT NOT NULL DEFAULT '?',
                clock TEXT NOT NULL DEFAULT '?',
                sids INTEGER NOT NULL DEFAULT 1,
                md5 TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                scanned_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sid_parent ON sid_metadata(parent);
            CREATE INDEX IF NOT EXISTS idx_sid_name_lower ON sid_metadata(name_lower);
            CREATE INDEX IF NOT EXISTS idx_sid_title_lower ON sid_metadata(title_lower);
            CREATE INDEX IF NOT EXISTS idx_sid_author_lower ON sid_metadata(author_lower);
            CREATE INDEX IF NOT EXISTS idx_sid_chip ON sid_metadata(chip);
            CREATE INDEX IF NOT EXISTS idx_sid_format ON sid_metadata(format);

            CREATE TABLE IF NOT EXISTS sid_index_runs (
                root TEXT PRIMARY KEY COLLATE NOCASE,
                mode TEXT NOT NULL,
                source_path TEXT NOT NULL DEFAULT '',
                completed TEXT NOT NULL,
                completed_at REAL NOT NULL,
                files INTEGER NOT NULL,
                parsed INTEGER NOT NULL,
                cached INTEGER NOT NULL,
                errors INTEGER NOT NULL DEFAULT 0,
                secs REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sid_scan_seen (
                scan_id TEXT NOT NULL,
                path TEXT NOT NULL COLLATE NOCASE,
                PRIMARY KEY(scan_id, path)
            );
            CREATE INDEX IF NOT EXISTS idx_sid_scan_seen ON sid_scan_seen(scan_id);

            INSERT INTO metadata(key, value) VALUES('schema_version', '4')
                ON CONFLICT(key) DO UPDATE SET value=excluded.value;
            COMMIT;
            """
        )
        # v3 adds persistent parse-error details without rebuilding existing
        # per-device databases. SQLite's ALTER TABLE is safe and fast here.
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(image_cache)")}
        if "parse_error" not in columns:
            self._conn.execute(
                "ALTER TABLE image_cache ADD COLUMN parse_error TEXT NOT NULL DEFAULT ''"
            )
        self._conn.execute(
            "INSERT INTO metadata(key,value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    @staticmethod
    def _full_path(parent: str, name: str) -> str:
        return ("" if parent == "/" else parent.rstrip("/")) + "/" + name

    @staticmethod
    def _scope_sql(column: str, root: str) -> tuple[str, tuple[str, ...]]:
        root = root.rstrip("/") or "/"
        if root == "/":
            return f"{column} LIKE '/%' COLLATE NOCASE", ()
        return (f"({column} = ? COLLATE NOCASE OR "
                f"{column} LIKE ? COLLATE NOCASE)"), (root, root + "/%")

    @staticmethod
    def _like_contains(value: str) -> str:
        escaped = value.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return "%" + escaped + "%"


    @staticmethod
    def mtime_equivalent(left: str, right: str, *, allow_timezone_shift: bool = False) -> bool:
        """Treat FAT/FTP timestamp precision and timezone offsets as equal.

        Local Windows scans store UTC MLSD-style timestamps. Some Ultimate
        firmware/FTP combinations expose the same FAT timestamp in local time,
        and FAT itself has two-second precision. Size and path must still match
        before this helper is used.
        """
        left, right = str(left or ""), str(right or "")
        if left == right:
            return True
        if not left or not right:
            return False
        try:
            a = time.mktime(time.strptime(left[:14], "%Y%m%d%H%M%S"))
            b = time.mktime(time.strptime(right[:14], "%Y%m%d%H%M%S"))
        except (ValueError, OverflowError):
            return False
        diff = abs(a - b)
        if diff <= 2.1:
            return True
        # Exact whole-hour shifts are characteristic of UTC/local-time
        # reporting rather than a file-content change. Only enable this for
        # explicit verification of a locally imported volume.
        return bool(allow_timezone_shift and diff <= 14 * 3600 + 2 and
                    min(diff % 3600, 3600 - (diff % 3600)) <= 2.1)

    def metadata_get(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM metadata WHERE key=?", (key,)
            ).fetchone()
        return str(row["value"]) if row else default

    def _metadata_set_locked(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def metadata_set(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._metadata_set_locked(key, value)

    def _put_directory_locked(self, path: str, entries: Iterable[dict], now: float) -> None:
        # Some Ultimate FTP firmware versions occasionally return the same
        # directory entry more than once (most often at the virtual root).
        # Legacy JSON caches can therefore also contain duplicates.  fs_entries
        # intentionally has a UNIQUE path, so collapse duplicates before the
        # batch insert rather than aborting the whole cache update/migration.
        rows_by_path: dict[str, tuple] = {}
        for e in entries:
            name = str(e.get("name", ""))
            if not name:
                continue
            full_path = self._full_path(path, name)
            row = (
                path,
                full_path,
                name,
                name.lower(),
                1 if e.get("dir") else 0,
                int(e.get("size", 0) or 0),
                str(e.get("mtime", "") or ""),
            )
            previous = rows_by_path.get(full_path)
            if previous is None:
                rows_by_path[full_path] = row
                continue

            # Prefer the richer duplicate record.  A directory classification
            # wins over a file classification, while non-zero size and a newer
            # MLSD modify timestamp are retained where available.
            rows_by_path[full_path] = (
                path,
                full_path,
                previous[2] or name,
                (previous[2] or name).lower(),
                max(int(previous[4]), int(row[4])),
                max(int(previous[5]), int(row[5])),
                max(str(previous[6]), str(row[6])),
            )

        rows = list(rows_by_path.values())
        self._conn.execute(
            "INSERT INTO directories(path,scanned_at) VALUES(?,?) "
            "ON CONFLICT(path) DO UPDATE SET scanned_at=excluded.scanned_at",
            (path, now),
        )
        self._conn.execute("DELETE FROM fs_entries WHERE parent=?", (path,))
        self._conn.executemany(
            "INSERT INTO fs_entries(parent,path,name,name_lower,is_dir,size,mtime) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET "
            "parent=excluded.parent,name=excluded.name,"
            "name_lower=excluded.name_lower,is_dir=excluded.is_dir,"
            "size=excluded.size,mtime=excluded.mtime",
            rows,
        )

    def put_directory(self, path: str, entries: Iterable[dict]) -> None:
        with self._lock, self._conn:
            self._put_directory_locked(path, entries, time.time())

    def get_directory(self, path: str) -> list[dict] | None:
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM directories WHERE path=?", (path,)
            ).fetchone()
            if not exists:
                return None
            rows = self._conn.execute(
                "SELECT name,is_dir,size,mtime FROM fs_entries WHERE parent=? "
                "ORDER BY is_dir DESC, name_lower",
                (path,),
            ).fetchall()
        return [
            {
                "name": r["name"],
                "dir": bool(r["is_dir"]),
                "size": int(r["size"]),
                "mtime": r["mtime"],
            }
            for r in rows
        ]

    def _put_image_locked(
        self,
        path: str,
        size: int,
        mtime: str,
        entries: Iterable[dict],
        *,
        parse_ok: bool,
        parse_error: str = "",
        now: float,
    ) -> None:
        size_i = int(size)
        mtime_s = str(mtime or "")
        rows = []
        for idx, e in enumerate(entries):
            name = str(e.get("name", ""))
            rows.append((
                path,
                size_i,
                mtime_s,
                idx,
                name,
                name.lower(),
                str(e.get("file_type", "") or ""),
                int(e.get("blocks", 0) or 0),
            ))
        self._conn.execute(
            "DELETE FROM image_cache WHERE path=? AND NOT (size=? AND mtime=?)",
            (path, size_i, mtime_s),
        )
        self._conn.execute(
            "INSERT INTO image_cache(path,size,mtime,scanned_at,last_access,parse_ok,parse_error) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(path,size,mtime) DO UPDATE SET "
            "scanned_at=excluded.scanned_at,last_access=excluded.last_access,"
            "parse_ok=excluded.parse_ok,parse_error=excluded.parse_error",
            (path, size_i, mtime_s, now, now, 1 if parse_ok else 0,
             str(parse_error or "")[:1000]),
        )
        self._conn.execute(
            "DELETE FROM image_entries WHERE image_path=? AND image_size=? AND image_mtime=?",
            (path, size_i, mtime_s),
        )
        self._conn.executemany(
            "INSERT INTO image_entries(image_path,image_size,image_mtime,entry_index,"
            "name,name_lower,file_type,blocks) VALUES(?,?,?,?,?,?,?,?)",
            rows,
        )

    def put_image(
        self,
        path: str,
        size: int,
        mtime: str,
        entries: Iterable[dict],
        *,
        parse_ok: bool = True,
        parse_error: str = "",
    ) -> None:
        with self._lock, self._conn:
            self._put_image_locked(
                path, size, mtime, entries, parse_ok=parse_ok,
                parse_error=parse_error, now=time.time()
            )

    def get_image(self, path: str, size: int, mtime: str) -> list[dict] | None:
        key = (path, int(size), str(mtime or ""))
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM image_cache WHERE path=? AND size=? AND mtime=?", key
            ).fetchone()
            if not row:
                return None
            rows = self._conn.execute(
                "SELECT entry_index,name,file_type,blocks FROM image_entries "
                "WHERE image_path=? AND image_size=? AND image_mtime=? "
                "ORDER BY entry_index",
                key,
            ).fetchall()
        return [
            {
                "name": r["name"],
                "file_type": r["file_type"],
                "blocks": int(r["blocks"]),
            }
            for r in rows
        ]

    def get_image_compatible(self, path: str, size: int, mtime: str, *, allow_timezone_shift: bool = False) -> list[dict] | None:
        """Return cached entries when only FAT/FTP timestamp representation differs."""
        exact = self.get_image(path, size, mtime)
        if exact is not None:
            return exact
        with self._lock:
            rows = self._conn.execute(
                "SELECT mtime FROM image_cache WHERE path=? AND size=? "
                "ORDER BY scanned_at DESC", (path, int(size))
            ).fetchall()
        for row in rows:
            old_mtime = str(row["mtime"] or "")
            if self.mtime_equivalent(old_mtime, mtime, allow_timezone_shift=allow_timezone_shift):
                return self.get_image(path, size, old_mtime)
        return None

    def parse_errors(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT path,size,mtime,parse_error FROM image_cache "
                "WHERE parse_ok=0 ORDER BY scanned_at DESC LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [{
            "path": row["path"], "size": int(row["size"]),
            "mtime": row["mtime"],
            "error": row["parse_error"] or "disk directory could not be parsed",
        } for row in rows]

    def has_image_path(self, path: str) -> bool:
        """Return whether any cached revision of *path* already exists."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM image_cache WHERE path=? LIMIT 1", (path,)
            ).fetchone()
        return row is not None

    def _set_index_root_locked(self, root: str, meta: dict) -> None:
        completed_at = float(meta.get("completed_at") or time.time())
        self._conn.execute(
            "INSERT INTO index_roots(root,completed,completed_at,dirs,images,secs) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(root) DO UPDATE SET "
            "completed=excluded.completed,completed_at=excluded.completed_at,"
            "dirs=excluded.dirs,images=excluded.images,secs=excluded.secs",
            (
                root,
                str(meta.get("completed", "")),
                completed_at,
                int(meta.get("dirs", 0)),
                int(meta.get("images", 0)),
                float(meta.get("secs", 0.0)),
            ),
        )

    def set_index_root(self, root: str, meta: dict) -> None:
        with self._lock, self._conn:
            self._set_index_root_locked(root, meta)

    def index_roots(self) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT root,completed,completed_at,dirs,images,secs FROM index_roots"
            ).fetchall()
        return {
            r["root"]: {
                "completed": r["completed"],
                "completed_at": float(r["completed_at"]),
                "dirs": int(r["dirs"]),
                "images": int(r["images"]),
                "secs": float(r["secs"]),
            }
            for r in rows
        }

    def complete_cover(self, root: str) -> dict | None:
        root = root.rstrip("/") or "/"
        root_key = root.casefold()
        candidates = []
        for indexed_root, meta in self.index_roots().items():
            prefix = indexed_root.rstrip("/") or "/"
            prefix_key = prefix.casefold()
            if (prefix == "/" or root_key == prefix_key or
                    root_key.startswith(prefix_key + "/")):
                candidates.append((len(prefix), meta))
        return max(candidates, default=(0, None), key=lambda x: x[0])[1]

    def search_cached(
        self,
        root: str,
        query: str,
        *,
        inside_images: bool,
        limit: int,
    ) -> list[dict]:
        query_like = self._like_contains(query)
        scope, args = self._scope_sql("path", root)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT path,name,is_dir,size FROM fs_entries "
                f"WHERE {scope} AND name_lower LIKE ? ESCAPE '\\' "
                "ORDER BY name_lower LIMIT ?",
                (*args, query_like, int(limit)),
            ).fetchall()
            results = [
                {
                    "kind": "dir" if r["is_dir"] else "file",
                    "path": r["path"],
                    "name": r["name"],
                    "size": int(r["size"]),
                }
                for r in rows
            ]
            remaining = max(0, int(limit) - len(results))
            if inside_images and remaining:
                iscope, iargs = self._scope_sql("image_path", root)
                irows = self._conn.execute(
                    f"SELECT image_path,entry_index,name,file_type,blocks "
                    f"FROM image_entries WHERE {iscope} "
                    "AND name_lower LIKE ? ESCAPE '\\' "
                    "ORDER BY name_lower LIMIT ?",
                    (*iargs, query_like, remaining),
                ).fetchall()
                results.extend(
                    {
                        "kind": "in-image",
                        "path": r["image_path"],
                        "name": r["name"],
                        "index": int(r["entry_index"]),
                        "file_type": r["file_type"],
                        "blocks": int(r["blocks"]),
                    }
                    for r in irows
                )
        return results

    def random_file(self, root: str, suffix: str) -> dict | None:
        """Return one random indexed file beneath *root* with *suffix*.

        The count + offset approach avoids SQLite's ORDER BY RANDOM() sorting
        the whole collection, which matters for full HVSC indexes.
        """
        root = root.rstrip("/") or "/"
        suffix = str(suffix or "").lower()
        if not suffix.startswith("."):
            suffix = "." + suffix
        scope, args = self._scope_sql("path", root)
        pattern = "%" + suffix
        with self._lock:
            count = int(self._conn.execute(
                f"SELECT COUNT(*) AS n FROM fs_entries "
                f"WHERE {scope} AND is_dir=0 AND name_lower LIKE ?",
                (*args, pattern),
            ).fetchone()["n"])
            if not count:
                return None
            import secrets
            offset = secrets.randbelow(count)
            row = self._conn.execute(
                f"SELECT parent,path,name,size,mtime FROM fs_entries "
                f"WHERE {scope} AND is_dir=0 AND name_lower LIKE ? "
                "ORDER BY path LIMIT 1 OFFSET ?",
                (*args, pattern, offset),
            ).fetchone()
        return ({
            "parent": row["parent"],
            "path": row["path"],
            "name": row["name"],
            "size": int(row["size"]),
            "mtime": row["mtime"],
            "candidates": count,
        } if row else None)

    def files_in_directory(self, parent: str, suffix: str, limit: int = 300) -> list[dict]:
        """Return indexed files in one directory, filtered by extension."""
        suffix = str(suffix or "").lower()
        if not suffix.startswith("."):
            suffix = "." + suffix
        with self._lock:
            rows = self._conn.execute(
                "SELECT path,name,size,mtime FROM fs_entries "
                "WHERE parent=? COLLATE NOCASE AND is_dir=0 AND name_lower LIKE ? "
                "ORDER BY name_lower LIMIT ?",
                (parent, "%" + suffix, max(1, int(limit))),
            ).fetchall()
        return [
            {"path": r["path"], "name": r["name"],
             "size": int(r["size"]), "mtime": r["mtime"]}
            for r in rows
        ]

    def files_below(self, root: str, suffix: str, limit: int = 0) -> list[dict]:
        """Return indexed files beneath ``root`` filtered by extension.

        ``limit=0`` means no explicit limit. This is primarily used by the
        SID metadata refresher, which can avoid walking the Ultimate over FTP
        when the storage catalogue already contains the collection.
        """
        suffix = str(suffix or "").lower()
        if not suffix.startswith("."):
            suffix = "." + suffix
        scope, args = self._scope_sql("path", root)
        sql = (
            "SELECT parent,path,name,size,mtime FROM fs_entries "
            f"WHERE {scope} AND is_dir=0 AND name_lower LIKE ? ORDER BY path"
        )
        params: tuple = (*args, "%" + suffix)
        if int(limit or 0) > 0:
            sql += " LIMIT ?"
            params = (*params, int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {"parent": r["parent"], "path": r["path"], "name": r["name"],
             "size": int(r["size"]), "mtime": r["mtime"]}
            for r in rows
        ]

    @staticmethod
    def _sid_row(path: str, size: int, mtime: str, meta: dict,
                 source: str, scanned_at: float) -> tuple:
        path = str(path)
        parent, _, name = path.rpartition("/")
        parent = parent or "/"
        name = name or str(meta.get("name") or "SID")
        title = str(meta.get("name") or name.rsplit(".", 1)[0])
        author = str(meta.get("author") or "")
        return (
            path, parent, name, name.lower(), int(size or 0), str(mtime or ""),
            str(meta.get("format") or ""), int(meta.get("version", 0) or 0),
            max(1, int(meta.get("songs", 1) or 1)),
            max(1, int(meta.get("start_song", 1) or 1)),
            title, title.lower(), author, author.lower(),
            str(meta.get("released") or ""), str(meta.get("chip") or "?"),
            str(meta.get("clock") or "?"), max(1, int(meta.get("sids", 1) or 1)),
            str(meta.get("md5") or ""), str(source or ""), float(scanned_at),
        )

    def put_sid_metadata(self, path: str, size: int, mtime: str,
                         meta: dict, *, source: str = "") -> None:
        self.put_sid_metadata_batch([{
            "path": path, "size": size, "mtime": mtime,
            "meta": meta, "source": source,
        }])

    def put_sid_metadata_batch(self, rows: Iterable[dict]) -> None:
        now = time.time()
        values = [
            self._sid_row(
                str(row.get("path") or ""), int(row.get("size", 0) or 0),
                str(row.get("mtime", "") or ""), dict(row.get("meta") or {}),
                str(row.get("source", "") or ""), now,
            )
            for row in rows if str(row.get("path") or "")
        ]
        if not values:
            return
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO sid_metadata(path,parent,name,name_lower,size,mtime,"
                "format,version,songs,start_song,title,title_lower,author,author_lower,"
                "released,chip,clock,sids,md5,source,scanned_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET parent=excluded.parent,"
                "name=excluded.name,name_lower=excluded.name_lower,"
                "size=CASE WHEN excluded.size>0 THEN excluded.size ELSE sid_metadata.size END,"
                "mtime=CASE WHEN excluded.mtime<>'' THEN excluded.mtime ELSE sid_metadata.mtime END,"
                "format=excluded.format,version=excluded.version,"
                "songs=excluded.songs,start_song=excluded.start_song,title=excluded.title,"
                "title_lower=excluded.title_lower,author=excluded.author,"
                "author_lower=excluded.author_lower,released=excluded.released,"
                "chip=excluded.chip,clock=excluded.clock,sids=excluded.sids,"
                "md5=CASE WHEN excluded.md5<>'' THEN excluded.md5 "
                "WHEN (excluded.size=0 OR excluded.size=sid_metadata.size) "
                "AND (excluded.mtime='' OR excluded.mtime=sid_metadata.mtime) "
                "THEN sid_metadata.md5 ELSE '' END,"
                "source=excluded.source,scanned_at=excluded.scanned_at",
                values,
            )

    def sid_metadata_get(self, path: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sid_metadata WHERE path=? COLLATE NOCASE", (path,)
            ).fetchone()
        return dict(row) if row else None

    def sid_metadata_for_paths(self, paths: Iterable[str]) -> dict[str, dict]:
        values = [str(path) for path in paths if str(path)]
        if not values:
            return {}
        out: dict[str, dict] = {}
        with self._lock:
            for start in range(0, len(values), 400):
                chunk = values[start:start + 400]
                marks = ",".join("?" for _ in chunk)
                rows = self._conn.execute(
                    f"SELECT * FROM sid_metadata WHERE path IN ({marks})",
                    chunk,
                ).fetchall()
                for row in rows:
                    out[str(row["path"]).casefold()] = dict(row)
        return out

    def sid_metadata_is_current(self, path: str, size: int, mtime: str) -> bool:
        row = self.sid_metadata_get(path)
        if not row:
            return False
        if int(size or 0) and int(row.get("size", 0) or 0) != int(size or 0):
            return False
        if str(mtime or "") and not self.mtime_equivalent(
                str(row.get("mtime", "") or ""), str(mtime or ""),
                allow_timezone_shift=True):
            return False
        return True

    def sid_metadata_count(self, root: str = "/") -> int:
        scope, args = self._scope_sql("path", root)
        with self._lock:
            return int(self._conn.execute(
                f"SELECT COUNT(*) AS n FROM sid_metadata WHERE {scope}", args
            ).fetchone()["n"])

    def sid_metadata_search(self, root: str, query: str = "", *,
                            chip: str = "all", sid_format: str = "all",
                            limit: int = 100) -> dict:
        scope, args = self._scope_sql("path", root)
        clauses = [scope]
        params: list[object] = list(args)
        terms = [term for term in str(query or "").lower().split() if term]
        for term in terms:
            pattern = self._like_contains(term)
            clauses.append(
                "(name_lower LIKE ? ESCAPE '\\' OR title_lower LIKE ? ESCAPE '\\' "
                "OR author_lower LIKE ? ESCAPE '\\' OR lower(path) LIKE ? ESCAPE '\\' "
                "OR lower(released) LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern] * 5)

        sid_format = str(sid_format or "all").upper()
        if sid_format in {"PSID", "RSID"}:
            clauses.append("format=?")
            params.append(sid_format)

        chip = str(chip or "all").lower()
        if chip == "6581":
            clauses.append("lower(chip)='6581' AND sids=1")
        elif chip == "8580":
            clauses.append("lower(chip)='8580' AND sids=1")
        elif chip == "either":
            clauses.append("lower(chip)='either' AND sids=1")
        elif chip in {"mixed", "multi", "mixed_multi"}:
            clauses.append("(sids>1 OR instr(chip,'+')>0)")
        elif chip == "unknown":
            clauses.append("(chip='' OR chip='?')")

        where = " AND ".join(f"({clause})" for clause in clauses)
        with self._lock:
            total = int(self._conn.execute(
                f"SELECT COUNT(*) AS n FROM sid_metadata WHERE {where}", params
            ).fetchone()["n"])
            rows = self._conn.execute(
                "SELECT path,parent,name,size,mtime,format,version,songs,start_song,"
                "title,author,released,chip,clock,sids,md5,source FROM sid_metadata "
                f"WHERE {where} ORDER BY CASE WHEN title_lower<>'' THEN title_lower "
                "ELSE name_lower END, path LIMIT ?",
                (*params, min(max(1, int(limit)), 300)),
            ).fetchall()
        return {"total": total, "results": [dict(row) for row in rows]}

    def begin_sid_scan(self, root: str, mode: str, source_path: str = "") -> str:
        import uuid
        scan_id = uuid.uuid4().hex
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM sid_scan_seen")
            self._metadata_set_locked("sid_scan_active", json.dumps({
                "scan_id": scan_id, "root": root, "mode": mode,
                "source_path": source_path, "started_at": time.time(),
            }))
        return scan_id

    def put_sid_scan_batch(self, scan_id: str, rows: Iterable[dict],
                           seen_paths: Iterable[str] = ()) -> None:
        rows = list(rows)
        self.put_sid_metadata_batch(rows)
        paths = [str(row.get("path") or "") for row in rows]
        paths.extend(str(path) for path in seen_paths)
        paths = [path for path in paths if path]
        if paths:
            with self._lock, self._conn:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO sid_scan_seen(scan_id,path) VALUES(?,?)",
                    ((scan_id, path) for path in paths),
                )

    def finish_sid_scan(self, scan_id: str, root: str, mode: str,
                        source_path: str, summary: dict) -> dict:
        scope, args = self._scope_sql("path", root)
        now = time.time()
        completed = time.strftime("%Y-%m-%d %H:%M")
        files = int(summary.get("files", 0))
        parsed = int(summary.get("parsed", 0))
        cached = int(summary.get("cached", 0))
        errors = int(summary.get("errors", 0))
        secs = float(summary.get("secs", 0.0))
        with self._lock, self._conn:
            self._conn.execute(
                f"DELETE FROM sid_metadata WHERE {scope} AND path NOT IN "
                "(SELECT path FROM sid_scan_seen WHERE scan_id=?)",
                (*args, scan_id),
            )
            self._conn.execute(
                "INSERT INTO sid_index_runs(root,mode,source_path,completed,"
                "completed_at,files,parsed,cached,errors,secs) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(root) DO UPDATE SET mode=excluded.mode,"
                "source_path=excluded.source_path,completed=excluded.completed,"
                "completed_at=excluded.completed_at,files=excluded.files,"
                "parsed=excluded.parsed,cached=excluded.cached,errors=excluded.errors,"
                "secs=excluded.secs",
                (root, mode, source_path, completed, now, files, parsed,
                 cached, errors, secs),
            )
            self._conn.execute("DELETE FROM sid_scan_seen WHERE scan_id=?", (scan_id,))
            self._metadata_set_locked("sid_scan_active", "")
        return {"root": root, "mode": mode, "source_path": source_path,
                "completed": completed, "files": files, "parsed": parsed,
                "cached": cached, "errors": errors, "secs": secs}

    def abort_sid_scan(self, scan_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM sid_scan_seen WHERE scan_id=?", (scan_id,))
            self._metadata_set_locked("sid_scan_active", "")

    def sid_index_runs(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT root,mode,source_path,completed,completed_at,files,parsed,"
                "cached,errors,secs FROM sid_index_runs ORDER BY completed_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def scope_counts(self, root: str) -> dict:
        dscope, dargs = self._scope_sql("path", root)
        iscope, iargs = self._scope_sql("path", root)
        with self._lock:
            directories = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM directories WHERE {dscope}", dargs
            ).fetchone()["n"]
            images = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM image_cache WHERE {iscope}", iargs
            ).fetchone()["n"]
        return {"directories": int(directories), "images": int(images)}

    def directory_count(self) -> int:
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) AS n FROM directories"
            ).fetchone()["n"])

    def stats(self) -> dict:
        with self._lock:
            directory_count = self._conn.execute(
                "SELECT COUNT(*) AS n FROM directories"
            ).fetchone()["n"]
            fs_count = self._conn.execute(
                "SELECT COUNT(*) AS n FROM fs_entries"
            ).fetchone()["n"]
            image_count = self._conn.execute(
                "SELECT COUNT(*) AS n FROM image_cache"
            ).fetchone()["n"]
            image_entry_count = self._conn.execute(
                "SELECT COUNT(*) AS n FROM image_entries"
            ).fetchone()["n"]
            parse_failure_count = self._conn.execute(
                "SELECT COUNT(*) AS n FROM image_cache WHERE parse_ok=0"
            ).fetchone()["n"]
            sid_metadata_count = self._conn.execute(
                "SELECT COUNT(*) AS n FROM sid_metadata"
            ).fetchone()["n"]
        disk_bytes = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                disk_bytes += self.path.with_name(self.path.name + suffix).stat().st_size
            except OSError:
                pass
        return {
            "directories": int(directory_count),
            "file_entries": int(fs_count),
            "images": int(image_count),
            "image_entries": int(image_entry_count),
            "parse_failures": int(parse_failure_count),
            "sid_metadata": int(sid_metadata_count),
            "disk_bytes": int(disk_bytes),
        }

    def invalidate_path(self, path: str) -> None:
        """Invalidate one directory and any completed root covering it."""
        path = path.rstrip("/") or "/"
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM directories WHERE path=?", (path,))
            roots = self._conn.execute("SELECT root FROM index_roots").fetchall()
            for row in roots:
                root = row["root"].rstrip("/") or "/"
                if root == "/" or path == root or path.startswith(root + "/"):
                    self._conn.execute("DELETE FROM index_roots WHERE root=?", (row["root"],))

    def clear_images(self) -> int:
        with self._lock, self._conn:
            n = int(self._conn.execute(
                "SELECT COUNT(*) AS n FROM image_cache"
            ).fetchone()["n"])
            self._conn.execute("DELETE FROM image_cache")
            # A completed root no longer has complete inside-image coverage.
            # Removing completion markers makes the next search re-open images.
            self._conn.execute("DELETE FROM index_roots")
        return n

    def clear_all(self) -> dict:
        with self._lock, self._conn:
            stats = self.stats()
            self._conn.execute("DELETE FROM index_roots")
            self._conn.execute("DELETE FROM image_cache")
            self._conn.execute("DELETE FROM directories")
            self._conn.execute("DELETE FROM local_imports")
            self._conn.execute("DELETE FROM scan_seen")
            self._conn.execute("DELETE FROM sid_metadata")
            self._conn.execute("DELETE FROM sid_index_runs")
            self._conn.execute("DELETE FROM sid_scan_seen")
        return stats

    def invalidate_completion(self, path: str) -> None:
        """Remove completion markers covering ``path`` without deleting data."""
        path = path.rstrip("/") or "/"
        with self._lock, self._conn:
            roots = self._conn.execute("SELECT root FROM index_roots").fetchall()
            for row in roots:
                root = row["root"].rstrip("/") or "/"
                if root == "/" or path == root or path.startswith(root + "/"):
                    self._conn.execute("DELETE FROM index_roots WHERE root=?", (row["root"],))

    def begin_local_scan(self, root: str, source_path: str) -> str:
        """Create a reconciliation session for a local USB import."""
        import uuid

        scan_id = uuid.uuid4().hex
        with self._lock, self._conn:
            # Old abandoned scan markers are never useful after a restart.
            self._conn.execute("DELETE FROM scan_seen")
            self._metadata_set_locked("local_scan_active", json.dumps({
                "scan_id": scan_id,
                "root": root,
                "source_path": source_path,
                "started_at": time.time(),
            }))
        self.invalidate_completion(root)
        return scan_id

    def put_local_batch(
        self,
        scan_id: str,
        directories: Iterable[tuple[str, list[dict]]],
        images: Iterable[dict],
        cached_image_paths: Iterable[str] = (),
    ) -> None:
        """Commit a batch from the local scanner in one SQLite transaction."""
        now = time.time()
        with self._lock, self._conn:
            for path, entries in directories:
                self._put_directory_locked(path, entries, now)
                self._conn.execute(
                    "INSERT OR IGNORE INTO scan_seen(scan_id,kind,path) VALUES(?,?,?)",
                    (scan_id, "dir", path),
                )
            for item in images:
                path = str(item["path"])
                self._put_image_locked(
                    path,
                    int(item.get("size", 0)),
                    str(item.get("mtime", "") or ""),
                    item.get("entries", ()),
                    parse_ok=bool(item.get("parse_ok", True)),
                    parse_error=str(item.get("parse_error", "") or ""),
                    now=now,
                )
                self._conn.execute(
                    "INSERT OR IGNORE INTO scan_seen(scan_id,kind,path) VALUES(?,?,?)",
                    (scan_id, "image", path),
                )
            self._conn.executemany(
                "INSERT OR IGNORE INTO scan_seen(scan_id,kind,path) VALUES(?,?,?)",
                ((scan_id, "image", str(path)) for path in cached_image_paths),
            )

    def finish_local_scan(
        self,
        scan_id: str,
        root: str,
        source_path: str,
        summary: dict,
        *,
        volume_id: str = "",
    ) -> dict:
        """Prune stale paths and publish a completed local import atomically."""
        root = root.rstrip("/") or "/"
        dscope, dargs = self._scope_sql("path", root)
        iscope, iargs = self._scope_sql("path", root)
        now = time.time()
        completed = time.strftime("%Y-%m-%d %H:%M")
        dirs = int(summary.get("dirs", 0))
        files = int(summary.get("files", 0))
        images = int(summary.get("images", 0)) + int(summary.get("images_cached", 0))
        errors = int(summary.get("errors", 0))
        secs = float(summary.get("secs", 0.0))
        with self._lock, self._conn:
            self._conn.execute(
                f"DELETE FROM image_cache WHERE {iscope} AND path NOT IN "
                "(SELECT path FROM scan_seen WHERE scan_id=? AND kind='image')",
                (*iargs, scan_id),
            )
            self._conn.execute(
                f"DELETE FROM directories WHERE {dscope} AND path NOT IN "
                "(SELECT path FROM scan_seen WHERE scan_id=? AND kind='dir')",
                (*dargs, scan_id),
            )
            self._set_index_root_locked(root, {
                "completed": completed,
                "completed_at": now,
                "dirs": dirs,
                "images": images,
                "secs": secs,
            })
            self._conn.execute(
                "INSERT INTO local_imports(root,source_path,volume_id,completed,"
                "completed_at,dirs,files,images,errors,secs) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(root) DO UPDATE SET source_path=excluded.source_path,"
                "volume_id=excluded.volume_id,completed=excluded.completed,"
                "completed_at=excluded.completed_at,dirs=excluded.dirs,"
                "files=excluded.files,images=excluded.images,errors=excluded.errors,"
                "secs=excluded.secs",
                (root, source_path, volume_id, completed, now, dirs, files,
                 images, errors, secs),
            )
            self._conn.execute("DELETE FROM scan_seen WHERE scan_id=?", (scan_id,))
            self._metadata_set_locked("local_scan_active", "")
        return {
            "root": root,
            "source_path": source_path,
            "completed": completed,
            "dirs": dirs,
            "files": files,
            "images": images,
            "errors": errors,
            "secs": secs,
        }

    def abort_local_scan(self, scan_id: str) -> None:
        """Discard reconciliation markers but retain safely committed results."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM scan_seen WHERE scan_id=?", (scan_id,))
            self._metadata_set_locked("local_scan_active", "")

    def local_imports(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT root,source_path,volume_id,completed,completed_at,dirs,"
                "files,images,errors,secs FROM local_imports "
                "ORDER BY completed_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def import_legacy(
        self,
        dir_cache_file: Path,
        image_cache_file: Path,
        index_meta_file: Path,
    ) -> dict:
        if self.metadata_get("legacy_import_complete") == "1":
            return {"imported": False, "already_done": True}

        counts = {"directories": 0, "images": 0, "roots": 0}

        def read_json(path: Path) -> dict:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                return value if isinstance(value, dict) else {}
            except (OSError, UnicodeError, json.JSONDecodeError):
                return {}

        dirs = read_json(dir_cache_file)
        images = read_json(image_cache_file)
        roots = read_json(index_meta_file)
        now = time.time()

        # One transaction makes a large 30k-directory migration much faster
        # than committing each imported folder separately.
        with self._lock, self._conn:
            for path, entries in dirs.items():
                if isinstance(path, str) and isinstance(entries, list):
                    self._put_directory_locked(path, entries, now)
                    counts["directories"] += 1

            for key, entries in images.items():
                if not isinstance(key, str) or not isinstance(entries, list):
                    continue
                try:
                    path, size, mtime = key.rsplit("|", 2)
                    size_i = int(size or 0)
                except (ValueError, TypeError):
                    continue
                self._put_image_locked(
                    path, size_i, mtime, entries, parse_ok=True, now=now
                )
                counts["images"] += 1

            for root, meta in roots.items():
                if isinstance(root, str) and isinstance(meta, dict):
                    self._set_index_root_locked(root, meta)
                    counts["roots"] += 1

            self._metadata_set_locked("legacy_import_complete", "1")
            self._metadata_set_locked("legacy_import_counts", json.dumps(counts))

        counts.update({"imported": bool(any(counts.values())), "already_done": False})
        return counts
