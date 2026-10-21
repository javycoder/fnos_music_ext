#!/usr/bin/env python3
"""Compute a stable fingerprint of the inputs that affect the application image.

The value is baked into the built image as a label (docker-compose.yml
build.labels) so startups can reuse `fnmusic-sources:latest` without probing
or pulling the Python base image when nothing that affects the build changed
(issue #70). Runtime switches in .env are deliberately excluded from the hash:
they are applied with a container restart, not a new image. The caller supplies the final `docker compose config --format json` output;
Compose itself owns environment interpolation and merge semantics.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
from pathlib import Path

FINGERPRINT_LABEL = "io.fnmusic-ext.build-fingerprint"
_COPY_RE = re.compile(r"^\s*COPY\s+(.+?)\s*$", re.MULTILINE | re.IGNORECASE)


def compose_build_inputs(repo: Path, config: dict) -> tuple[Path, Path, dict]:
    build = dict(config["services"]["fnmusic-sources"]["build"])
    raw_labels = build.get("labels") or {}
    labels = (dict(item.split("=", 1) for item in raw_labels)
              if isinstance(raw_labels, list) else dict(raw_labels))
    labels.pop(FINGERPRINT_LABEL, None)
    build["labels"] = labels
    context = (repo / build.get("context", ".")).resolve()
    if not context.is_relative_to(repo):
        raise ValueError("build context escapes repository")
    dockerfile = context / build.get("dockerfile", "Dockerfile")
    if not dockerfile.resolve().is_relative_to(context):
        raise ValueError("Dockerfile escapes build context")
    # Absolute project paths must not make identical checkouts hash differently.
    build["context"] = context.relative_to(repo).as_posix()
    return context, dockerfile, build


def copy_sources(repo: Path, context_dir: Path, dockerfile: Path) -> list[Path]:
    sources: set[Path] = set()
    text = re.sub(r"\\\n", " ", dockerfile.read_text(encoding="utf-8"))
    for match in _COPY_RE.finditer(text):
        instruction = match.group(1).strip()
        if re.match(r"--from(?:=|\s)", instruction):
            continue
        instruction = re.sub(r"^(?:--[^\s]+\s+)+", "", instruction)
        fields = json.loads(instruction) if instruction.startswith("[") else shlex.split(instruction)
        if len(fields) < 2:
            raise ValueError(f"invalid COPY instruction: {instruction}")
        for source in fields[:-1]:
            if Path(source).is_absolute() or ".." in Path(source).parts:
                raise ValueError(f"COPY source escapes repository/build context: {source}")
            # glob includes hidden entries (unlike glob.glob on older Python).
            matches = list(context_dir.glob(source))
            if not matches:
                raise ValueError(f"COPY source missing: {source}")
            for path in matches:
                if not path.resolve().is_relative_to(context_dir):
                    raise ValueError(f"COPY source escapes repository/build context: {source}")
                if not path.exists():
                    raise ValueError(f"COPY source missing: {source}")
                sources.add(path)
    return sorted(sources)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _compile_ignore(pattern: str) -> re.Pattern[str]:
    # Anchored at the context root; `**` spans directories, `*` stays in one.
    parts = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            parts.append("(?:[^/]+/)*")
            i += 3
        elif pattern[i:i + 2] == "**":
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(parts))


def dockerignore_rules(path: Path) -> list[tuple[re.Pattern[str], bool, bool]]:
    # (pattern, negate, dir_only); the last matching rule wins, like .dockerignore.
    rules: list[tuple[re.Pattern[str], bool, bool]] = []
    if not path.is_file():
        return rules
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        if negate:
            line = line[1:].strip()
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        if line:
            rules.append((_compile_ignore(line), negate, dir_only))
    return rules


def _ignored(rel: str, is_dir: bool,
             rules: list[tuple[re.Pattern[str], bool, bool]]) -> bool:
    parts = rel.split("/")
    prefixes = ["/".join(parts[:i]) for i in range(1, len(parts))]

    def matches(rule: tuple[re.Pattern[str], bool, bool]) -> bool:
        regex, _negate, dir_only = rule
        if not dir_only:
            return regex.fullmatch(rel) is not None or any(regex.fullmatch(prefix) for prefix in prefixes)
        if is_dir and regex.fullmatch(rel) is not None:
            return True
        return any(regex.fullmatch(prefix) for prefix in prefixes)

    ignored = False
    for rule in rules:
        if matches(rule):
            ignored = not rule[1]
    return ignored


def path_entries(repo: Path, path: Path,
                 rules: list[tuple[re.Pattern[str], bool, bool]]) -> list[tuple[str, str]]:
    entries = []

    def visit(candidate: Path, ancestors: set[Path]) -> None:
        rel = candidate.relative_to(repo).as_posix()
        if _ignored(rel, candidate.is_dir(), rules):
            return
        resolved = candidate.resolve()
        if not resolved.is_relative_to(repo):
            raise ValueError(f"COPY symlink escapes build context: {rel}")
        if resolved in ancestors:
            raise ValueError(f"COPY symlink cycle: {rel}")
        if candidate.is_symlink():
            entries.append((rel + ":symlink", os.readlink(candidate)))
        if candidate.is_dir():
            entries.append((rel + ":directory", str(candidate.stat().st_mode & 0o777)))
            for child in sorted(candidate.iterdir()):
                visit(child, ancestors | {resolved})
        elif candidate.is_file():
            entries.append((rel, file_hash(candidate)))
            entries.append((rel + ":mode", str(candidate.stat().st_mode & 0o777)))
        else:
            raise ValueError(f"COPY source missing or unsupported: {rel}")

    visit(path, set())
    return entries


def compute(repo: Path, config: dict) -> str:
    repo = repo.resolve()
    context_dir, dockerfile, build = compose_build_inputs(repo, config)
    ignorefile = Path(str(dockerfile) + ".dockerignore")
    if not ignorefile.is_file():
        ignorefile = context_dir / ".dockerignore"
    rules = dockerignore_rules(ignorefile)
    canonical = [
        ("build", json.dumps(build, sort_keys=True, separators=(",", ":"))),
        ("dockerfile", file_hash(dockerfile)),
        ("dockerignore", file_hash(ignorefile) if ignorefile.is_file() else ""),
    ]
    entries = set()
    for source in copy_sources(repo, context_dir, dockerfile):
        entries.update(path_entries(context_dir, source, rules))
    canonical.extend(sorted(entries))
    return hashlib.sha256(json.dumps(canonical, separators=(",", ":")).encode()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--config", type=Path, help="resolved compose JSON (default: stdin)")
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text() if args.config else sys.stdin.read())
        print(compute(args.repo, config))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"ERROR: cannot compute build fingerprint: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
