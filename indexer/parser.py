"""
AST-based code parser using tree-sitter.

Chunks at semantic boundaries (functions, classes, hooks, components, interfaces)
rather than naive character count. Each chunk carries full context metadata so
the LLM knows exactly where it is in the codebase.

Languages: TypeScript/TSX/JS/JSX (tree-sitter-typescript) and Python (tree-sitter-python).
Each chunk is tagged with its source `repo` so cross-repo retrieval is unambiguous.
"""
import os
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import tree_sitter_python as tspy
import tree_sitter_typescript as tsts
from tree_sitter import Language, Parser, Node

TS_LANGUAGE = Language(tsts.language_typescript())
TSX_LANGUAGE = Language(tsts.language_tsx())
PY_LANGUAGE = Language(tspy.language())

_ts_parser = Parser(TS_LANGUAGE)
_tsx_parser = Parser(TSX_LANGUAGE)
_py_parser = Parser(PY_LANGUAGE)

TS_EXTS = {".ts", ".tsx", ".js", ".jsx"}
PY_EXTS = {".py"}
DOC_EXTS = {".md", ".mdx", ".markdown"}
CODE_EXTS = TS_EXTS | PY_EXTS
SUPPORTED_EXTS = CODE_EXTS | DOC_EXTS


@dataclass
class CodeChunk:
    content: str
    file_path: str          # relative to repo root
    chunk_type: str         # function | component | hook | class | method | interface | type
    name: str
    repo: str = ""          # repo name from repos.py (empty for legacy single-repo indexes)
    parent_class: Optional[str] = None
    signature: Optional[str] = None
    start_line: int = 0
    end_line: int = 0
    imports: list[str] = field(default_factory=list)
    language: str = ""      # "typescript" | "python"

    @property
    def id(self) -> str:
        parts = [self.repo, self.file_path]
        if self.parent_class:
            parts.append(self.parent_class)
        parts.append(self.name)
        return "::".join(parts)

    def to_metadata(self) -> dict:
        return {
            "repo": self.repo,
            "language": self.language,
            "file_path": self.file_path,
            "chunk_type": self.chunk_type,
            "name": self.name,
            "parent_class": self.parent_class or "",
            "start_line": self.start_line,
            "end_line": self.end_line,
        }

    def to_document(self) -> str:
        """Rich text representation fed to the embedding model.
        Prepending metadata makes semantic search significantly more accurate."""
        header = (
            f"// Repo: {self.repo} | File: {self.file_path} | "
            f"Type: {self.chunk_type} | Name: {self.name}"
        )
        if self.parent_class:
            header += f" | Class: {self.parent_class}"
        if self.imports:
            header += f"\n// Imports: {'; '.join(self.imports[:3])}"
        return f"{header}\n\n{self.content}"


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _child_text(node: Node, child_type: str, source: bytes) -> Optional[str]:
    for child in node.children:
        if child.type == child_type:
            return _text(child, source)
    return None


# ── TypeScript / JavaScript ──────────────────────────────────────────────────

def _extract_imports_ts(root: Node, source: bytes) -> list[str]:
    return [
        _text(child, source)
        for child in root.children
        if child.type == "import_statement"
    ]


def _infer_type_ts(name: str, node_type: str) -> str:
    if node_type == "interface_declaration":
        return "interface"
    if node_type == "type_alias_declaration":
        return "type"
    if node_type == "class_declaration":
        return "class"
    if name and name.startswith("use") and len(name) > 3 and name[3].isupper():
        return "hook"
    if name and name[0].isupper():
        return "component"
    return "function"


def _signature_ts(node: Node, source: bytes) -> str:
    text = _text(node, source)
    lines = text.split("\n")
    sig = []
    for line in lines:
        sig.append(line)
        if "=>" in line or "{" in line or ";" in line:
            break
    return "\n".join(sig)[:300]


def _traverse_ts(
    node: Node,
    source: bytes,
    chunks: list,
    file_path: str,
    imports: list[str],
    repo: str,
    parent_class: Optional[str] = None,
):
    t = node.type

    if t in ("function_declaration", "generator_function_declaration"):
        name = _child_text(node, "identifier", source)
        if name:
            chunks.append(CodeChunk(
                content=_text(node, source),
                file_path=file_path,
                chunk_type=_infer_type_ts(name, t),
                name=name,
                repo=repo,
                parent_class=parent_class,
                signature=_signature_ts(node, source),
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                imports=imports,
                language="typescript",
            ))
        return

    if t == "class_declaration":
        class_name = None
        for child in node.children:
            if child.type == "type_identifier":
                class_name = _text(child, source)
                break
        if class_name:
            content = _text(node, source)
            chunks.append(CodeChunk(
                content=content[:3000],
                file_path=file_path,
                chunk_type="class",
                name=class_name,
                repo=repo,
                parent_class=parent_class,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                imports=imports,
                language="typescript",
            ))
            for child in node.children:
                _traverse_ts(child, source, chunks, file_path, imports, repo, parent_class=class_name)
        return

    if t == "method_definition":
        name = _child_text(node, "property_identifier", source)
        if name and parent_class:
            chunks.append(CodeChunk(
                content=_text(node, source),
                file_path=file_path,
                chunk_type="method",
                name=name,
                repo=repo,
                parent_class=parent_class,
                signature=_signature_ts(node, source),
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                imports=imports,
                language="typescript",
            ))
        return

    if t == "lexical_declaration":
        for declarator in node.children:
            if declarator.type != "variable_declarator":
                continue
            name_node = None
            value_node = None
            for child in declarator.children:
                if child.type == "identifier" and name_node is None:
                    name_node = child
                elif child.type in ("arrow_function", "function"):
                    value_node = child
            if name_node and value_node:
                name = _text(name_node, source)
                chunks.append(CodeChunk(
                    content=_text(node, source),
                    file_path=file_path,
                    chunk_type=_infer_type_ts(name, "function_declaration"),
                    name=name,
                    repo=repo,
                    parent_class=parent_class,
                    signature=_text(node, source).split("\n")[0][:300],
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    imports=imports,
                    language="typescript",
                ))
        return

    if t in ("interface_declaration", "type_alias_declaration"):
        name = None
        for child in node.children:
            if child.type in ("type_identifier", "identifier"):
                name = _text(child, source)
                break
        if name:
            chunks.append(CodeChunk(
                content=_text(node, source),
                file_path=file_path,
                chunk_type=_infer_type_ts(name, t),
                name=name,
                repo=repo,
                parent_class=parent_class,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                imports=imports,
                language="typescript",
            ))
        return

    if t == "export_statement":
        for child in node.children:
            _traverse_ts(child, source, chunks, file_path, imports, repo, parent_class)
        return

    for child in node.children:
        _traverse_ts(child, source, chunks, file_path, imports, repo, parent_class)


# ── Python ──────────────────────────────────────────────────────────────────

def _extract_imports_py(root: Node, source: bytes) -> list[str]:
    out: list[str] = []
    for child in root.children:
        if child.type in ("import_statement", "import_from_statement"):
            out.append(_text(child, source))
    return out


def _py_def_name(node: Node, source: bytes) -> Optional[str]:
    """Return the identifier name of a function_definition or class_definition."""
    name_node = node.child_by_field_name("name")
    return _text(name_node, source) if name_node else None


def _py_signature(node: Node, source: bytes) -> str:
    """First line of `def …(…):` up to and including the colon."""
    text = _text(node, source)
    first_line = text.split("\n", 1)[0]
    return first_line[:300]


def _traverse_py(
    node: Node,
    source: bytes,
    chunks: list,
    file_path: str,
    imports: list[str],
    repo: str,
    parent_class: Optional[str] = None,
):
    t = node.type

    # `decorated_definition` wraps a function/class with @decorators — unwrap and continue.
    if t == "decorated_definition":
        # The actual definition is the last child (function_definition or class_definition)
        for child in node.children:
            if child.type in ("function_definition", "class_definition"):
                _traverse_py(child, source, chunks, file_path, imports, repo, parent_class)
        return

    if t == "function_definition":
        name = _py_def_name(node, source)
        if name:
            chunk_type = "method" if parent_class else "function"
            chunks.append(CodeChunk(
                content=_text(node, source)[:3000],
                file_path=file_path,
                chunk_type=chunk_type,
                name=name,
                repo=repo,
                parent_class=parent_class,
                signature=_py_signature(node, source),
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                imports=imports,
                language="python",
            ))
        return  # don't descend into function bodies

    if t == "class_definition":
        name = _py_def_name(node, source)
        if name:
            chunks.append(CodeChunk(
                content=_text(node, source)[:3000],
                file_path=file_path,
                chunk_type="class",
                name=name,
                repo=repo,
                parent_class=parent_class,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                imports=imports,
                language="python",
            ))
            # Walk class body for methods.
            body = node.child_by_field_name("body")
            if body:
                for child in body.children:
                    _traverse_py(child, source, chunks, file_path, imports, repo, parent_class=name)
        return

    for child in node.children:
        _traverse_py(child, source, chunks, file_path, imports, repo, parent_class)


# ── File-level chunks ────────────────────────────────────────────────────────
# A "file" chunk is a synthetic per-file overview: the file head (imports,
# module docstring, top constants) plus a manifest of every symbol the
# semantic chunker found below. It gives the retriever something to match for
# "what does X module do" / "where is auth handled" — queries that don't name a
# specific function and previously had no good chunk to land on.

def _file_head(source_text: str, max_lines: int = 60, max_chars: int = 1500) -> str:
    """First N lines of the file, capped by char count.
    Captures shebang, module docstring, imports, top-level constants — enough
    for the embedding model to recognize the file's purpose."""
    out: list[str] = []
    used = 0
    for line in source_text.split("\n")[:max_lines]:
        used += len(line) + 1
        if used > max_chars:
            break
        out.append(line)
    return "\n".join(out)


def _symbol_manifest(symbol_chunks: list["CodeChunk"]) -> str:
    if not symbol_chunks:
        return "Symbols: (none)"
    rows = ["Symbols:"]
    for c in symbol_chunks:
        if c.chunk_type == "file":
            continue
        prefix = f"{c.parent_class}." if c.parent_class else ""
        sig = (c.signature or "").split("\n", 1)[0].strip()
        # If we have a usable signature, show it; else just the name.
        if sig and sig != c.name:
            rows.append(f"  [{c.chunk_type}] {prefix}{c.name}  L{c.start_line}  {sig[:120]}")
        else:
            rows.append(f"  [{c.chunk_type}] {prefix}{c.name}  L{c.start_line}")
    return "\n".join(rows)


def _make_file_chunk(
    rel_path: str,
    source_text: str,
    repo_name: str,
    language: str,
    symbol_chunks: list["CodeChunk"],
    imports: list[str],
) -> "CodeChunk":
    head = _file_head(source_text)
    manifest = _symbol_manifest(symbol_chunks)
    content = f"{head}\n\n---\n{manifest}"
    return CodeChunk(
        content=content[:4000],
        file_path=rel_path,
        chunk_type="file",
        # Use the basename so lookup_symbol("jira.py") finds it; semantic
        # search reads the full content.
        name=Path(rel_path).name,
        repo=repo_name,
        signature=None,
        start_line=1,
        end_line=len(source_text.split("\n")),
        imports=imports,
        language=language,
    )


# ── Markdown ─────────────────────────────────────────────────────────────────
# Split markdown by heading. Each section becomes one chunk so the retriever
# can land on a specific section ("# Authentication") rather than a whole doc.

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _split_markdown_sections(text: str) -> list[tuple[str, str, int]]:
    """Return list of (heading, body, start_line).
    A leading body before the first heading is grouped under "Introduction".
    Headings inside fenced code blocks are ignored."""
    sections: list[tuple[str, list[str], int]] = []
    in_fence = False
    current_heading = "Introduction"
    current_lines: list[str] = []
    current_start = 1

    for i, line in enumerate(text.split("\n"), 1):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            current_lines.append(line)
            continue

        m = _HEADING_RE.match(line) if not in_fence else None
        if m:
            if current_lines:
                sections.append((current_heading, current_lines, current_start))
            current_heading = m.group(2).strip()
            current_lines = []
            current_start = i
        else:
            current_lines.append(line)

    if current_lines:
        sections.append((current_heading, current_lines, current_start))

    out: list[tuple[str, str, int]] = []
    for heading, body_lines, start in sections:
        body = "\n".join(body_lines).strip()
        if body:
            out.append((heading, body, start))
    return out


def _parse_markdown(rel_path: str, text: str, repo_name: str) -> list["CodeChunk"]:
    sections = _split_markdown_sections(text)
    if not sections and text.strip():
        # File with no headings — emit one chunk for the whole file.
        return [CodeChunk(
            content=text[:2500],
            file_path=rel_path,
            chunk_type="doc",
            name=Path(rel_path).name,
            repo=repo_name,
            start_line=1,
            end_line=len(text.split("\n")),
            language="markdown",
        )]

    chunks: list[CodeChunk] = []
    for heading, body, start_line in sections:
        content = f"# {heading}\n\n{body}"[:2500]
        chunks.append(CodeChunk(
            content=content,
            file_path=rel_path,
            chunk_type="doc",
            name=heading[:120],
            repo=repo_name,
            start_line=start_line,
            end_line=start_line + body.count("\n"),
            language="markdown",
        ))
    return chunks


# ── Public API ──────────────────────────────────────────────────────────────

def parse_file(file_path: str, repo_root: str, repo_name: str = "") -> list[CodeChunk]:
    path = Path(file_path)
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTS:
        return []

    try:
        rel_path = str(path.relative_to(repo_root))
    except ValueError:
        rel_path = str(path)

    # ── Markdown: heading-split chunks, no AST ──
    if ext in DOC_EXTS:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            print(f"  [skip] {rel_path}: {e}")
            return []
        return _parse_markdown(rel_path, text, repo_name)

    # ── Code: AST-based symbol chunks + a synthetic file chunk ──
    source = path.read_bytes()
    source_text = source.decode("utf-8", errors="replace")
    chunks: list[CodeChunk] = []

    if ext in TS_EXTS:
        parser = _tsx_parser if ext in (".tsx", ".jsx") else _ts_parser
        tree = parser.parse(source)
        imports = _extract_imports_ts(tree.root_node, source)
        _traverse_ts(tree.root_node, source, chunks, rel_path, imports, repo_name)
        language = "typescript"
    else:  # PY_EXTS
        tree = _py_parser.parse(source)
        imports = _extract_imports_py(tree.root_node, source)
        _traverse_py(tree.root_node, source, chunks, rel_path, imports, repo_name)
        language = "python"

    # Prepend the file-level overview chunk. Even files with zero symbols
    # (e.g. config modules, __init__.py) get one — that's the whole point.
    file_chunk = _make_file_chunk(rel_path, source_text, repo_name, language, chunks, imports)
    return [file_chunk] + chunks


# Common dirs to ignore — language-agnostic. Add more as we encounter them.
_IGNORED_DIRS = {
    "node_modules", ".git", "dist", "build", "coverage", "__pycache__",
    ".next", ".cache", ".venv", "venv", "env", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "site-packages", "target", ".idea",
    ".vscode", ".gitlab", "htmlcov", "_build",
}

# Path fragments to skip — for boilerplate/scaffolding that's identical across
# every repo and would otherwise dominate top-K with near-duplicates.
# Tuple of POSIX path fragments matched as substrings against rel_path.
_IGNORED_PATH_FRAGMENTS = (
    ".specify/templates",       # spec-kit project templates (plan/spec/tasks/etc.)
    ".github/ISSUE_TEMPLATE",
    ".github/PULL_REQUEST_TEMPLATE",
)


def _path_excluded(rel_posix: str) -> bool:
    return any(frag in rel_posix for frag in _IGNORED_PATH_FRAGMENTS)


def parse_repo(repo_path: str, repo_name: str = "") -> list[CodeChunk]:
    """
    Walk repo_path, parse every supported file, return tagged chunks.

    `repo_name` ends up in chunk.repo / chunk.id / chunk.metadata. Pass the
    name from repos.py — empty string is allowed for backward compat but
    breaks multi-repo retrieval.
    """
    repo_path = str(Path(repo_path).resolve())
    all_chunks: list[CodeChunk] = []

    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in _IGNORED_DIRS]
        for file in files:
            if Path(file).suffix.lower() not in SUPPORTED_EXTS:
                continue
            full = os.path.join(root, file)
            rel_posix = Path(full).relative_to(repo_path).as_posix()
            if _path_excluded(rel_posix):
                continue
            try:
                chunks = parse_file(full, repo_path, repo_name)
                all_chunks.extend(chunks)
            except Exception as e:
                print(f"  [skip] {file}: {e}")

    return all_chunks
