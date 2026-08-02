from __future__ import annotations

import ast
import fnmatch
import itertools
import json
import re
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any, TypeAlias

from tgv.exceptions import RepositoryAccessError
from tgv.models import (
    CodeSymbol,
    Evidence,
    IndexDiagnostics,
    OmittedSummary,
    RepositoryConfig,
    RepositorySummary,
)

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{2,}")
IndexEntry: TypeAlias = dict[str, Any]
Index: TypeAlias = dict[str, IndexEntry]


def _symbol(
    kind: str, name: str, path: str, node: ast.AST, parent: str | None = None
) -> dict[str, object]:
    return CodeSymbol(
        name=name,
        kind=kind,
        path=path,
        start_line=getattr(node, "lineno", 1),
        end_line=getattr(node, "end_lineno", None),
        parent=parent,
    ).model_dump()


def _assignment_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    names: list[str] = []
    for target in targets:
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            names.extend(item.id for item in target.elts if isinstance(item, ast.Name))
    return names


def extract_python_symbols(text: str, path: str) -> list[dict[str, object]]:
    """Extract reviewable Python symbol metadata; syntax errors safely return no symbols."""
    try:
        tree = ast.parse(text, filename=path)
    except (SyntaxError, ValueError, TypeError):
        return []
    symbols: list[dict[str, object]] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            symbols.append(_symbol("class", node.name, path, node))
            for child in node.body:
                if isinstance(child, ast.AsyncFunctionDef):
                    symbols.append(_symbol("async_method", child.name, path, child, node.name))
                elif isinstance(child, ast.FunctionDef):
                    symbols.append(_symbol("method", child.name, path, child, node.name))
        elif isinstance(node, ast.AsyncFunctionDef):
            symbols.append(_symbol("async_function", node.name, path, node))
        elif isinstance(node, ast.FunctionDef):
            symbols.append(_symbol("function", node.name, path, node))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            symbols.extend(
                _symbol("assignment", name, path, node) for name in _assignment_names(node)
            )
        elif isinstance(node, ast.Import):
            symbols.extend(_symbol("import", alias.name, path, node) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            symbols.append(_symbol("import", module, path, node))
            symbols.extend(
                _symbol("import", alias.name, path, node, module or None) for alias in node.names
            )
    return symbols


class Repository:
    """Bounded, read-only repository access with strict root confinement."""

    def __init__(self, root: Path, config: RepositoryConfig, deny_patterns: list[str]) -> None:
        try:
            self.root = root.expanduser().resolve(strict=True)
        except OSError as exc:
            raise RepositoryAccessError(f"Repository root is not a directory: {root}") from exc
        if not self.root.is_dir():
            raise RepositoryAccessError(f"Repository root is not a directory: {self.root}")
        self.config = config
        self.deny_patterns = deny_patterns
        self._index: Index | None = None
        self._omitted = OmittedSummary()
        self._diagnostics = IndexDiagnostics()

    def _relative(self, path: Path) -> Path:
        try:
            return path.resolve(strict=False).relative_to(self.root)
        except (OSError, ValueError) as exc:
            raise RepositoryAccessError("Path escapes repository root") from exc

    def _resolve_user_path(self, relative_path: str) -> tuple[Path, Path]:
        candidate = PurePosixPath(relative_path.replace("\\", "/"))
        if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
            raise RepositoryAccessError("Path escapes repository root")
        unresolved = self.root.joinpath(*candidate.parts)
        try:
            resolved = unresolved.resolve(strict=True)
            relative = resolved.relative_to(self.root)
        except (OSError, ValueError) as exc:
            raise RepositoryAccessError("Path escapes repository root or does not exist") from exc
        return resolved, relative

    def _denied(self, relative: Path) -> bool:
        posix = relative.as_posix()
        return any(
            fnmatch.fnmatch(posix, pattern)
            or (pattern.startswith("**/") and fnmatch.fnmatch(posix, pattern[3:]))
            for pattern in self.deny_patterns
        )

    def _eligibility(self, path: Path) -> str | None:
        try:
            relative = self._relative(path)
        except RepositoryAccessError:
            return "unsafe_symlink"
        if any(part in self.config.ignored_directories for part in relative.parts):
            return "ignored"
        if path.suffix.lower() in self.config.ignored_suffixes or self._denied(relative):
            return "denied" if self._denied(relative) else "ignored"
        try:
            if not path.is_file():
                return "ignored"
            if path.stat().st_size > self.config.maximum_file_bytes:
                return "too_large"
        except OSError:
            return "ignored"
        return None

    def _eligible(self, path: Path) -> bool:
        return self._eligibility(path) is None

    def files(self) -> Iterator[Path]:
        self._omitted = OmittedSummary()
        try:
            paths = sorted(self.root.rglob("*"))
        except OSError as exc:
            raise RepositoryAccessError(f"Cannot enumerate repository root: {self.root}") from exc
        for path in paths:
            reason = self._eligibility(path)
            if reason is None:
                yield path
                continue
            try:
                should_count = path.is_file() or path.is_symlink()
            except OSError:
                should_count = True
            if should_count:
                setattr(self._omitted, reason, getattr(self._omitted, reason) + 1)

    def list_files(self, limit: int | None = None) -> list[str]:
        maximum = limit if limit is not None else self.config.maximum_results
        return [self._relative(path).as_posix() for path in list(self.files())[:maximum]]

    def read_text(self, relative_path: str) -> str:
        path, relative = self._resolve_user_path(relative_path)
        if not self._eligible(path):
            raise RepositoryAccessError("File is denied, missing, binary, or too large")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise RepositoryAccessError(f"Cannot read {relative.as_posix()}") from exc
        if b"\x00" in data[:8192]:
            raise RepositoryAccessError("Binary file is not readable as text")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RepositoryAccessError("File is not UTF-8 text") from exc

    def excerpt(
        self, relative_path: str, start_line: int = 1, line_count: int | None = None
    ) -> str:
        lines = self.read_text(relative_path).splitlines()
        start = max(1, start_line)
        count = line_count if line_count is not None else self.config.excerpt_lines
        return "\n".join(
            f"{number:>6}: {line}"
            for number, line in enumerate(lines[start - 1 : start - 1 + count], start)
        )

    def _index_file(self) -> Path:
        target = (
            self.config.index_path
            if self.config.index_path.is_absolute()
            else self.root / self.config.index_path
        )
        try:
            target.resolve(strict=False).relative_to(self.root)
        except ValueError as exc:
            raise RepositoryAccessError(
                "Repository index path must remain inside repository root"
            ) from exc
        return target

    def _load_cache(self) -> Index:
        try:
            raw = json.loads(self._index_file().read_text(encoding="utf-8"))
            files = raw.get("files", {})
            return files if raw.get("version") == 2 and isinstance(files, dict) else {}
        except (OSError, ValueError, TypeError, RepositoryAccessError):
            return {}

    @staticmethod
    def _entry(path: Path, rel: str, text: str, stat: Any) -> IndexEntry:
        frequencies: Counter[str] = Counter()
        positions: dict[str, list[int]] = {}
        for number, line in enumerate(text.splitlines(), 1):
            for token in (match.lower() for match in _TOKEN.findall(line)):
                frequencies[token] += 1
                positions.setdefault(token, []).append(number)
        symbols = extract_python_symbols(text, rel) if path.suffix.lower() == ".py" else []
        return {
            "path": rel,
            "extension": path.suffix.lower(),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "tokens": dict(sorted(frequencies.items())),
            "lines": {key: value[:100] for key, value in sorted(positions.items())},
            "symbols": symbols,
        }

    def build_index(self) -> Index:
        started = time.perf_counter()
        old = self._load_cache()
        current: Index = {}
        cache_hits = 0
        cache_misses = 0
        for path in self.files():
            rel = self._relative(path).as_posix()
            try:
                stat = path.stat()
            except OSError:
                continue
            cached = old.get(rel)
            if (
                cached
                and cached.get("size") == stat.st_size
                and cached.get("mtime_ns") == stat.st_mtime_ns
                and "symbols" in cached
            ):
                current[rel] = cached
                cache_hits += 1
                continue
            cache_misses += 1
            try:
                text = self.read_text(rel)
            except RepositoryAccessError:
                self._omitted.binary_or_encoding += 1
                continue
            current[rel] = self._entry(path, rel, text, stat)
        self._index = current
        self._diagnostics = IndexDiagnostics(
            files_indexed=len(current),
            files_skipped=sum(self._omitted.model_dump().values()),
            symbols_extracted=sum(len(item.get("symbols", [])) for item in current.values()),
            build_seconds=round(time.perf_counter() - started, 6),
            cache_hits=cache_hits,
            cache_misses=cache_misses,
        )
        try:
            target = self._index_file()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps({"version": 2, "files": current}, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
        except (OSError, RepositoryAccessError):
            pass
        return current

    @staticmethod
    def _rank(
        rel: str, meta: IndexEntry, terms: list[str]
    ) -> tuple[float, list[str], list[str], list[str], int]:
        token_hits = [term for term in terms if term in meta["tokens"]]
        path_hits = [term for term in terms if term in rel.lower()]
        symbol_hits = sorted(
            {
                str(symbol["name"])
                for symbol in meta.get("symbols", [])
                if str(symbol["name"]).lower() in terms
            }
        )
        import_hits = sorted(
            {
                str(symbol["name"])
                for symbol in meta.get("symbols", [])
                if symbol.get("kind") == "import"
                and any(term in str(symbol["name"]).lower() for term in terms)
            }
        )
        hit_lines = sorted({n for term in token_hits for n in meta["lines"].get(term, [])})
        first = (
            hit_lines[0]
            if hit_lines
            else min(
                (
                    int(s["start_line"])
                    for s in meta.get("symbols", [])
                    if str(s["name"]) in symbol_hits
                ),
                default=1,
            )
        )
        proximity = 0.0
        if len(hit_lines) > 1:
            proximity = 2.0 / (
                1 + min(right - left for left, right in itertools.pairwise(hit_lines))
            )
        frequency = sum(min(meta["tokens"].get(term, 0), 20) for term in token_hits)
        extension_bonus = (
            0.5 if meta["extension"] in {".py", ".toml", ".yaml", ".yml", ".md"} else 0.0
        )
        score = (
            len(path_hits) * 5
            + len(token_hits) * 3
            + len(symbol_hits) * 8
            + len(import_hits) * 4
            + frequency * 0.25
            + proximity
            + extension_bonus
        )
        reasons: list[str] = []
        if path_hits:
            reasons.append("path match")
        if token_hits:
            reasons.append("exact token match")
        if symbol_hits:
            reasons.append("exact symbol match")
        if import_hits:
            reasons.append("import match")
        if proximity:
            reasons.append("nearby line hits")
        return score, sorted(set(path_hits + token_hits)), symbol_hits, reasons, first

    def search(self, query: str, limit: int | None = None) -> list[Evidence]:
        started = time.perf_counter()
        terms = list(dict.fromkeys(token.lower() for token in _TOKEN.findall(query)))[:12]
        if not terms:
            return []
        try:
            index = self.build_index() if self.config.index_enabled else self._scan_index()
        except (OSError, RepositoryAccessError, ValueError, TypeError):
            index = self._scan_index()
        ranked: list[tuple[float, Evidence]] = []
        for rel, meta in index.items():
            score, matched_terms, matched_symbols, reasons, first = self._rank(rel, meta, terms)
            if not reasons:
                continue
            start = max(1, first - 3)
            try:
                excerpt = self.excerpt(rel, start, min(self.config.excerpt_lines, 60))
            except RepositoryAccessError:
                continue
            end = start + max(0, len(excerpt.splitlines()) - 1)
            ranked.append(
                (
                    score,
                    Evidence(
                        path=rel,
                        start_line=start,
                        end_line=end,
                        reason="; ".join(reasons),
                        excerpt=excerpt,
                        score=round(score, 4),
                        matched_terms=matched_terms,
                        matched_symbols=matched_symbols,
                        ranking_reasons=reasons,
                    ),
                )
            )
        ranked.sort(key=lambda item: (-item[0], item[1].path, item[1].start_line))
        self._diagnostics.search_seconds = round(time.perf_counter() - started, 6)
        maximum = limit if limit is not None else self.config.maximum_results
        return [evidence for _, evidence in ranked[:maximum]]

    def _scan_index(self) -> Index:
        result: Index = {}
        for path in self.files():
            rel = self._relative(path).as_posix()
            try:
                result[rel] = self._entry(path, rel, self.read_text(rel), path.stat())
            except (RepositoryAccessError, OSError):
                continue
        return result

    def diagnostics(self) -> IndexDiagnostics:
        if self._index is None:
            self.build_index()
        return self._diagnostics.model_copy()

    def omitted_summary(self) -> OmittedSummary:
        if self._index is None:
            self.build_index()
        return self._omitted.model_copy()

    def summary(self) -> RepositorySummary:
        paths = list(self.files())
        extensions = Counter(path.suffix.lower() or "[no extension]" for path in paths)
        indexed = len(
            self._index
            if self._index is not None
            else (self.build_index() if self.config.index_enabled else {})
        )
        return RepositorySummary(
            root=self.root,
            file_count=len(paths),
            sampled_extensions=dict(extensions.most_common(12)),
            indexed_file_count=indexed,
        )
