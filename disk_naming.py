"""Read-only disk-image naming analysis and constrained local grouping rules.

The analyser operates only on rows already present in u64deck's SQLite storage
index.  It never walks storage, renames files, mounts images, or accepts user
regular expressions.  Approved rules use a small validated model that the
runtime disk-swap matcher can apply safely.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Callable, Iterable

SUPPORTED_EXTENSIONS = (".d64", ".d71", ".d81", ".g64")
_ALLOWED_DELIMITERS = {"-", "_", "."}


def normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def split_image_name(filename: str) -> tuple[str, str] | None:
    name = str(filename or "").rsplit("/", 1)[-1]
    stem, dot, ext = name.rpartition(".")
    ext = "." + ext.casefold() if dot else ""
    if ext not in SUPPORTED_EXTENSIONS or not stem.strip():
        return None
    return stem.strip(), ext


def validate_rule(rule: dict) -> dict:
    """Return a normalised constrained rule or raise ValueError."""
    kind = str(rule.get("kind") or "")
    if kind != "terminal-letter":
        raise ValueError("unsupported disk-grouping rule kind")
    delimiter = str(rule.get("delimiter") or "")
    if delimiter not in _ALLOWED_DELIMITERS:
        raise ValueError("unsupported terminal delimiter")
    tokens = sorted({str(v).casefold() for v in (rule.get("tokens") or [])})
    if len(tokens) < 2 or any(not re.fullmatch(r"[a-z]", token) for token in tokens):
        raise ValueError("terminal-letter rules require at least two letter markers")
    extensions = sorted({str(v).casefold() for v in (rule.get("extensions") or [])})
    if not extensions or any(ext not in SUPPORTED_EXTENSIONS for ext in extensions):
        raise ValueError("unsupported disk-image extension in rule")
    scope = str(rule.get("scope") or "/").strip() or "/"
    if not scope.startswith("/") or "\x00" in scope:
        raise ValueError("rule scope must be an Ultimate path")
    return {
        "kind": kind,
        "delimiter": delimiter,
        "tokens": tokens,
        "extensions": extensions,
        "scope": scope.rstrip("/") or "/",
    }


def rule_pattern_key(rule: dict) -> str:
    clean = validate_rule(rule)
    return ":".join((
        clean["kind"],
        clean["delimiter"].encode("unicode_escape").decode("ascii"),
        ",".join(clean["tokens"]),
        ",".join(clean["extensions"]),
    ))


def path_in_scope(parent: str, scope: str) -> bool:
    parent_key = (str(parent or "/").rstrip("/") or "/").casefold()
    scope_key = (str(scope or "/").rstrip("/") or "/").casefold()
    return scope_key == "/" or parent_key == scope_key or parent_key.startswith(scope_key + "/")


def custom_signature(filename: str, rule: dict, parent: str = "/"):
    """Return a family/token tuple for one approved constrained rule."""
    if not bool(rule.get("enabled", True)):
        return None
    try:
        clean = validate_rule(rule)
    except ValueError:
        return None
    if not path_in_scope(parent, clean["scope"]):
        return None
    split = split_image_name(filename)
    if not split:
        return None
    stem, ext = split
    if ext not in clean["extensions"]:
        return None
    delimiter = re.escape(clean["delimiter"])
    match = re.fullmatch(
        rf"(?P<base>.+?){delimiter}(?P<token>[A-Za-z])",
        stem,
    )
    if not match:
        return None
    token = match.group("token").casefold()
    if token not in clean["tokens"]:
        return None
    title = normalize_title(match.group("base"))
    if not title:
        return None
    return (
        ext,
        "custom-terminal-letter",
        int(rule.get("id") or 0),
        title,
        clean["delimiter"],
        tuple(clean["tokens"]),
    ), (1, token)


def _has_unsuffixed_sibling(base: str, ext: str, sibling_names: Iterable[str]) -> bool:
    title = normalize_title(base)
    for name in sibling_names:
        split = split_image_name(name)
        if not split:
            continue
        stem, candidate_ext = split
        if candidate_ext == ext and normalize_title(stem) == title:
            return True
    return False


def group_with_rule(current_name: str, sibling_names: Iterable[str], rule: dict,
                    parent: str = "/") -> list[str]:
    current_name = str(current_name or "").rsplit("/", 1)[-1]
    current = custom_signature(current_name, rule, parent)
    if not current:
        return [current_name]
    family, current_token = current
    clean = validate_rule(rule)
    split = split_image_name(current_name)
    assert split is not None
    stem, ext = split
    base = stem[:-(len(clean["delimiter"]) + 1)]
    siblings = list(sibling_names)
    # The unsuffixed sibling veto is deliberately non-overridable for reusable
    # rules. A one-off exact-set override is the explicit escape hatch.
    if _has_unsuffixed_sibling(base, ext, siblings):
        return [current_name]

    matches: list[tuple[tuple, str]] = []
    seen = set()
    for name in siblings:
        signature = custom_signature(name, rule, parent)
        if not signature or signature[0] != family:
            continue
        folded = str(name).casefold()
        if folded in seen:
            continue
        seen.add(folded)
        matches.append((signature[1], str(name)))
    if current_name.casefold() not in seen:
        matches.append((current_token, current_name))
    if len(matches) < 2:
        return [current_name]
    matches.sort(key=lambda item: (item[0], item[1].casefold()))
    return [name for _token, name in matches]


def _pattern_label(kind: str, delimiter: str = "", tokens: Iterable[str] = ()) -> str:
    if kind == "terminal-letter":
        token_text = "/".join(tokens)
        return f"terminal {delimiter}{token_text}"
    labels = {
        "marked": "explicit Disk/Disc/Side/Part/Volume marker",
        "marked-untitled": "title-less Disk/Disc/Side/Part/Volume marker",
        "of-total": "N of M marker",
        "numbered": "separator-delimited number",
        "bare-wrapped": "parenthesised token",
        "lettered-hyphen": "terminal -a/-b",
    }
    return labels.get(kind, kind.replace("-", " "))


def _candidate_id(pattern_key: str) -> str:
    return hashlib.sha1(pattern_key.encode("utf-8")).hexdigest()[:12]


def exact_set_id(parent: str, names: Iterable[str]) -> str:
    """Return a stable identifier for one exact ordered filename family."""
    clean_parent = str(parent or "/").rstrip("/") or "/"
    clean_names = [str(name) for name in names if str(name)]
    material = clean_parent.casefold() + "\0" + "\0".join(
        name.casefold() for name in clean_names
    )
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


def _exact_set_record(parent: str, names: Iterable[str]) -> dict:
    ordered = [str(name) for name in names if str(name)]
    return {
        "set_id": exact_set_id(parent, ordered),
        "parent": str(parent or "/").rstrip("/") or "/",
        "names": ordered,
    }


def _set_record(parent: str, names: list[str], base: str, ext: str) -> dict:
    record = _exact_set_record(parent, sorted(names, key=str.casefold))
    record.update({"base": base, "extension": ext})
    return record


def analyse_disk_names(
    rows: Iterable[dict],
    builtin_signature: Callable[[str], object],
    *,
    rules: Iterable[dict] = (),
    overrides: Iterable[dict] = (),
    max_examples: int = 8,
    include_all_sets: bool = False,
) -> dict:
    """Analyse indexed filenames without touching storage or index contents."""
    by_parent: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        name = str(row.get("name") or "")
        if split_image_name(name):
            by_parent[str(row.get("parent") or "/")].append(dict(row))

    enabled_rules = [dict(rule) for rule in rules if bool(rule.get("enabled", True))]
    enabled_overrides = [dict(item) for item in overrides if bool(item.get("enabled", True))]
    recognised: dict[str, dict] = {}
    claimed: set[str] = set()
    rejected_sets: list[dict] = []

    def add_recognised(label: str, parent: str, names: list[str]):
        if len(names) < 2:
            return
        bucket = recognised.setdefault(label, {
            "pattern": label, "sets": 0, "files": 0, "examples": []
        })
        bucket["sets"] += 1
        bucket["files"] += len(names)
        if len(bucket["examples"]) < max_examples:
            bucket["examples"].append(_exact_set_record(parent, names[:8]))
        for name in names:
            claimed.add((parent + "/" + name).casefold())

    # Exact local overrides are user intent and take precedence in reporting.
    for override in enabled_overrides:
        parent = str(override.get("parent") or "/")
        names = [str(v) for v in (override.get("names") or [])]
        available = {row["name"].casefold(): row["name"] for row in by_parent.get(parent, [])}
        present = [available[name.casefold()] for name in names if name.casefold() in available]
        if len(present) >= 2:
            add_recognised("approved exact set", parent, present)

    # Built-in families.
    for parent, parent_rows in by_parent.items():
        families: dict[tuple, list[tuple[tuple, str]]] = defaultdict(list)
        names = [row["name"] for row in parent_rows]
        for row in parent_rows:
            path_key = (parent + "/" + row["name"]).casefold()
            if path_key in claimed:
                continue
            sig = builtin_signature(row["name"])
            if sig:
                families[sig[0]].append((sig[1], row["name"]))
        for family, members in families.items():
            unique = {name.casefold(): (token, name) for token, name in members}
            if len(unique) < 2:
                continue
            ordered = [name for _token, name in sorted(unique.values(), key=lambda v: (v[0], v[1].casefold()))]
            kind = family[1] if len(family) > 1 else "built-in"
            # Built-in bare wrapped and terminal -a/-b rules retain the
            # unsuffixed sibling veto.
            if kind in {"bare-wrapped", "lettered-hyphen"}:
                title = family[2]
                ext = family[0]
                if any(
                    split_image_name(name)
                    and split_image_name(name)[1] == ext
                    and builtin_signature(name) is None
                    and normalize_title(split_image_name(name)[0]) == title
                    for name in names
                ):
                    rejected_sets.append({
                        "reason": "unsuffixed sibling veto",
                        "parent": parent,
                        "names": ordered,
                    })
                    continue
            add_recognised(_pattern_label(kind), parent, ordered)

    # Approved reusable rules.
    for rule in enabled_rules:
        label = "approved " + _pattern_label(
            str(rule.get("kind") or ""),
            str(rule.get("delimiter") or ""),
            rule.get("tokens") or [],
        )
        for parent, parent_rows in by_parent.items():
            names = [row["name"] for row in parent_rows]
            seen_families = set()
            for name in names:
                key = (parent + "/" + name).casefold()
                if key in claimed:
                    continue
                sig = custom_signature(name, rule, parent)
                if not sig or sig[0] in seen_families:
                    continue
                grouped = group_with_rule(name, names, rule, parent)
                seen_families.add(sig[0])
                if len(grouped) >= 2:
                    add_recognised(label, parent, grouped)

    candidate_sets: dict[tuple[str, tuple[str, ...]], list[dict]] = defaultdict(list)
    ambiguous: dict[str, dict] = {}
    rejected: dict[str, dict] = {}
    ambiguous_folders: dict[str, dict] = {}

    def add_summary(target: dict[str, dict], label: str, parent: str, names: list[str], reason: str = ""):
        ordered = sorted(names, key=str.casefold)
        record = _exact_set_record(parent, ordered)
        example = {**record, "names": ordered[:8], "total_files": len(ordered)}
        bucket = target.setdefault(label, {
            "pattern": label, "sets": 0, "files": 0, "examples": [], "reason": reason,
        })
        bucket["sets"] += 1
        bucket["files"] += len(ordered)
        if include_all_sets:
            bucket.setdefault("all_sets", []).append(record)
        if len(bucket["examples"]) < max_examples:
            bucket["examples"].append(example)
        if target is ambiguous:
            folder = ambiguous_folders.setdefault(record["parent"], {
                "parent": record["parent"], "sets": 0, "files": 0,
            })
            folder["sets"] += 1
            folder["files"] += len(ordered)

    for item in rejected_sets:
        add_summary(rejected, item["reason"], item["parent"], item["names"], item["reason"])

    for parent, parent_rows in by_parent.items():
        remaining = [row for row in parent_rows
                     if (parent + "/" + row["name"]).casefold() not in claimed]
        all_names = [row["name"] for row in parent_rows]
        plain_lookup = {(split_image_name(name)[1], normalize_title(split_image_name(name)[0]))
                        for name in all_names if split_image_name(name)}

        terminal: dict[tuple, list[tuple[str, str]]] = defaultdict(list)
        bracketed: dict[tuple, list[str]] = defaultdict(list)
        glued_numbers: dict[tuple, list[str]] = defaultdict(list)
        spaced_letters: dict[tuple, list[str]] = defaultdict(list)

        # Built-in marker-like names can still be unsafe when one folder mixes
        # conventions for the same title, for example Game-Disk1 and
        # Game-Side2. Surface those explicitly rather than silently omitting
        # them from the report.
        marker_titles: dict[tuple, list[tuple[tuple, str]]] = defaultdict(list)
        for row in remaining:
            signature = builtin_signature(row["name"])
            if not signature:
                continue
            family = signature[0]
            if len(family) < 3 or family[1] == "marked-untitled":
                continue
            marker_titles[(family[0], family[2])].append((family, row["name"]))
        mixed_names = set()
        for (_ext, _title), values in marker_titles.items():
            families = {value[0][1:4] for value in values}
            names = sorted({name.casefold(): name for _family, name in values}.values(), key=str.casefold)
            if len(names) >= 2 and len(families) >= 2:
                add_summary(rejected, "mixed marker families", parent, names,
                            "The same title uses conflicting disk/side/number conventions.")
                mixed_names.update(name.casefold() for name in names)

        for row in remaining:
            if row["name"].casefold() in mixed_names:
                continue
            name = row["name"]
            split = split_image_name(name)
            if not split:
                continue
            stem, ext = split
            m = re.fullmatch(r"(?P<base>.+?)(?P<delimiter>[-_.])(?P<token>[A-Za-z])", stem)
            if m:
                terminal[(ext, m.group("base"), m.group("delimiter"))].append(
                    (m.group("token").casefold(), name)
                )
                continue
            m = re.fullmatch(r"(?P<base>.+?)\[(?P<token>[A-Za-z])\]", stem)
            if m:
                bracketed[(ext, m.group("base"))].append(name)
                continue
            m = re.fullmatch(r"(?P<base>.*?[^\d\W])(?P<token>\d+)", stem)
            if m:
                glued_numbers[(ext, m.group("base"))].append(name)
                continue
            m = re.fullmatch(r"(?P<base>.+?)\s+(?P<token>[A-Za-z])", stem)
            if m:
                spaced_letters[(ext, m.group("base"))].append(name)

        for (ext, base, delimiter), values in terminal.items():
            tokens = sorted({token for token, _name in values})
            names = sorted({name.casefold(): name for _token, name in values}.values(), key=str.casefold)
            if len(tokens) < 2 or len(names) < 2:
                continue
            if (ext, normalize_title(base)) in plain_lookup:
                add_summary(rejected, "unsuffixed sibling veto", parent, names,
                            "A matching unsuffixed image exists; reusable grouping is unsafe.")
                continue
            candidate_sets[(delimiter, tuple(tokens))].append(
                _set_record(parent, names, base, ext)
            )

        for (_ext, _base), names in bracketed.items():
            if len(names) >= 2:
                add_summary(rejected, "GoodTools [a]/[b] alternatives", parent, names,
                            "Square-bracket letters are alternate-dump markers, not disk sides.")
        for (_ext, _base), names in glued_numbers.items():
            if len(names) >= 2:
                add_summary(ambiguous, "glued trailing numbers", parent, names,
                            "These may be sequels or separate releases.")
        for (_ext, _base), names in spaced_letters.items():
            if len(names) >= 2:
                add_summary(ambiguous, "bare trailing A/B", parent, names,
                            "A bare final letter can be part of a title or edition name.")

    candidates = []
    for (delimiter, tokens_tuple), sets in sorted(candidate_sets.items()):
        extensions = sorted({item["extension"] for item in sets})
        clean = validate_rule({
            "kind": "terminal-letter",
            "delimiter": delimiter,
            "tokens": list(tokens_tuple),
            "extensions": extensions,
            "scope": "/",
        })
        pattern_key = rule_pattern_key(clean)
        candidates.append({
            "candidate_id": _candidate_id(pattern_key),
            "pattern_key": pattern_key,
            "pattern": _pattern_label(clean["kind"], clean["delimiter"], clean["tokens"]),
            "kind": clean["kind"],
            "delimiter": clean["delimiter"],
            "tokens": clean["tokens"],
            "extensions": clean["extensions"],
            "sets": len(sets),
            "files": sum(len(item["names"]) for item in sets),
            "examples": sets[:max_examples],
            "conflicts": 0,
        })

    recognised_rows = sorted(recognised.values(), key=lambda row: (-row["sets"], row["pattern"]))
    ambiguous_rows = sorted(ambiguous.values(), key=lambda row: (-row["sets"], row["pattern"]))
    rejected_rows = sorted(rejected.values(), key=lambda row: (-row["sets"], row["pattern"]))
    total_files = sum(len(rows) for rows in by_parent.values())
    summary = {
        "indexed_disk_images": total_files,
        "directories": len(by_parent),
        "recognised_sets": sum(row["sets"] for row in recognised_rows),
        "recognised_files": sum(row["files"] for row in recognised_rows),
        "candidate_sets": sum(row["sets"] for row in candidates),
        "candidate_files": sum(row["files"] for row in candidates),
        "ambiguous_sets": sum(row["sets"] for row in ambiguous_rows),
        "rejected_sets": sum(row["sets"] for row in rejected_rows),
    }

    lines = [
        "u64deck disk-image naming analysis",
        f"Indexed disk images: {summary['indexed_disk_images']}",
        f"Recognised sets: {summary['recognised_sets']}",
        f"Unrecognised high-confidence sets: {summary['candidate_sets']}",
        f"Ambiguous sets: {summary['ambiguous_sets']}",
        f"Rejected sets: {summary['rejected_sets']}",
        "",
    ]
    for heading, items in (
        ("Recognised", recognised_rows),
        ("High-confidence candidates", candidates),
        ("Ambiguous", ambiguous_rows),
        ("Rejected", rejected_rows),
    ):
        lines.append(heading)
        lines.append("-" * len(heading))
        if not items:
            lines.append("None")
        for item in items:
            lines.append(f"{item['pattern']}: {item['sets']} sets / {item['files']} files")
            for example in item.get("examples", [])[:3]:
                lines.append(f"  {example['parent']}: " + " | ".join(example["names"]))
        lines.append("")

    return {
        "summary": summary,
        "recognised": recognised_rows,
        "candidates": candidates,
        "ambiguous": ambiguous_rows,
        "rejected": rejected_rows,
        "ambiguous_folders": sorted(ambiguous_folders.values(), key=lambda row: row["parent"].casefold()),
        "rules": list(rules),
        "overrides": list(overrides),
        "report_text": "\n".join(lines).rstrip() + "\n",
    }


def candidate_by_key(report: dict, pattern_key: str) -> dict | None:
    return next((item for item in report.get("candidates", [])
                 if item.get("pattern_key") == pattern_key), None)


def serialise_rule_examples(candidate: dict) -> str:
    return json.dumps(candidate.get("examples", [])[:8], ensure_ascii=False, separators=(",", ":"))
