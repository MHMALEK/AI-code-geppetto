"""
AST-based code parser using tree-sitter.

Chunks at semantic boundaries (functions, classes, hooks, components, interfaces)
rather than naive character count. Each chunk carries full context metadata so
the LLM knows exactly where it is in the codebase.
"""
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import tree_sitter_typescript as tsts
from tree_sitter import Language, Parser, Node

TS_LANGUAGE = Language(tsts.language_typescript())
TSX_LANGUAGE = Language(tsts.language_tsx())

_ts_parser = Parser(TS_LANGUAGE)
_tsx_parser = Parser(TSX_LANGUAGE)


@dataclass
class CodeChunk:
    content: str
    file_path: str          # relative to repo root
    chunk_type: str         # function | component | hook | class | method | interface | type
    name: str
    parent_class: Optional[str] = None
    signature: Optional[str] = None
    start_line: int = 0
    end_line: int = 0
    imports: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        parts = [self.file_path]
        if self.parent_class:
            parts.append(self.parent_class)
        parts.append(self.name)
        return "::".join(parts)

    def to_metadata(self) -> dict:
        return {
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
        header = f"// File: {self.file_path} | Type: {self.chunk_type} | Name: {self.name}"
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


def _extract_imports(root: Node, source: bytes) -> list[str]:
    return [
        _text(child, source)
        for child in root.children
        if child.type == "import_statement"
    ]


def _infer_type(name: str, node_type: str) -> str:
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


def _signature(node: Node, source: bytes) -> str:
    text = _text(node, source)
    lines = text.split("\n")
    sig = []
    for line in lines:
        sig.append(line)
        if "=>" in line or "{" in line or ";" in line:
            break
    return "\n".join(sig)[:300]


def _traverse(
    node: Node,
    source: bytes,
    chunks: list,
    file_path: str,
    imports: list[str],
    parent_class: Optional[str] = None,
):
    t = node.type

    # ── Named function declaration ────────────────────────────────────────────
    if t in ("function_declaration", "generator_function_declaration"):
        name = _child_text(node, "identifier", source)
        if name:
            chunks.append(CodeChunk(
                content=_text(node, source),
                file_path=file_path,
                chunk_type=_infer_type(name, t),
                name=name,
                parent_class=parent_class,
                signature=_signature(node, source),
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                imports=imports,
            ))
        return  # don't recurse — nested functions get their own pass via export_statement

    # ── Class ─────────────────────────────────────────────────────────────────
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
                parent_class=parent_class,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                imports=imports,
            ))
            for child in node.children:
                _traverse(child, source, chunks, file_path, imports, parent_class=class_name)
        return

    # ── Class method ──────────────────────────────────────────────────────────
    if t == "method_definition":
        name = _child_text(node, "property_identifier", source)
        if name and parent_class:
            chunks.append(CodeChunk(
                content=_text(node, source),
                file_path=file_path,
                chunk_type="method",
                name=name,
                parent_class=parent_class,
                signature=_signature(node, source),
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                imports=imports,
            ))
        return

    # ── const Foo = () => {} / const Foo = function() {} ─────────────────────
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
                    chunk_type=_infer_type(name, "function_declaration"),
                    name=name,
                    parent_class=parent_class,
                    signature=_text(node, source).split("\n")[0][:300],
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    imports=imports,
                ))
        return

    # ── Interface / Type alias ────────────────────────────────────────────────
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
                chunk_type=_infer_type(name, t),
                name=name,
                parent_class=parent_class,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                imports=imports,
            ))
        return

    # ── export statement — unwrap and recurse ─────────────────────────────────
    if t == "export_statement":
        for child in node.children:
            _traverse(child, source, chunks, file_path, imports, parent_class)
        return

    # ── default: recurse ──────────────────────────────────────────────────────
    for child in node.children:
        _traverse(child, source, chunks, file_path, imports, parent_class)


def parse_file(file_path: str, repo_root: str) -> list[CodeChunk]:
    path = Path(file_path)
    ext = path.suffix.lower()
    if ext not in (".ts", ".tsx", ".js", ".jsx"):
        return []

    source = path.read_bytes()
    parser = _tsx_parser if ext in (".tsx", ".jsx") else _ts_parser
    tree = parser.parse(source)

    try:
        rel_path = str(path.relative_to(repo_root))
    except ValueError:
        rel_path = str(path)

    imports = _extract_imports(tree.root_node, source)
    chunks: list[CodeChunk] = []
    _traverse(tree.root_node, source, chunks, rel_path, imports)
    return chunks


def parse_repo(repo_path: str) -> list[CodeChunk]:
    repo_path = str(Path(repo_path).resolve())
    _ignored = {"node_modules", ".git", "dist", "build", "coverage", "__pycache__", ".next", ".cache"}
    all_chunks: list[CodeChunk] = []

    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in _ignored]
        for file in files:
            if Path(file).suffix.lower() in (".ts", ".tsx", ".js", ".jsx"):
                try:
                    chunks = parse_file(os.path.join(root, file), repo_path)
                    all_chunks.extend(chunks)
                except Exception as e:
                    print(f"  [skip] {file}: {e}")

    return all_chunks
