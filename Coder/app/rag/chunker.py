from dataclasses import dataclass
from pathlib import Path

import tiktoken

try:
    from tree_sitter_languages import get_language, get_parser
    _TS_AVAILABLE = True
except Exception:
    _TS_AVAILABLE = False

LANGUAGE_MAP: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".jsx": "javascript",
    ".html": "html",
    ".css": "css",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".java": "java",
}

# Tree-sitter node types that represent meaningful semantic units
SEMANTIC_NODE_TYPES: dict[str, list[str]] = {
    "python": ["function_definition", "class_definition", "decorated_definition"],
    "javascript": ["function_declaration", "function_expression", "arrow_function", "class_declaration", "method_definition"],
    "typescript": ["function_declaration", "function_expression", "arrow_function", "class_declaration", "method_definition"],
    "tsx": ["function_declaration", "function_expression", "arrow_function", "class_declaration", "method_definition"],
    "go": ["function_declaration", "method_declaration", "type_declaration"],
    "rust": ["function_item", "impl_item", "struct_item", "enum_item"],
    "c": ["function_definition", "struct_specifier"],
    "cpp": ["function_definition", "class_specifier", "struct_specifier"],
    "java": ["method_declaration", "class_declaration", "interface_declaration"],
    "html": [],
    "css": ["rule_set", "media_statement"],
}

MAX_TOKENS = 512
OVERLAP_TOKENS = 50

_tokenizer = tiktoken.get_encoding("cl100k_base")


@dataclass
class Chunk:
    content: str
    file_path: str
    start_line: int
    end_line: int
    language: str
    chunk_index: int = 0


def _token_count(text: str) -> int:
    return len(_tokenizer.encode(text))


def _split_by_tokens(text: str, start_line: int, file_path: str, language: str, base_index: int) -> list[Chunk]:
    """Fall back to token-window sliding chunks for large or non-code content."""
    tokens = _tokenizer.encode(text)
    lines = text.splitlines()
    chunks: list[Chunk] = []
    step = MAX_TOKENS - OVERLAP_TOKENS
    idx = base_index

    for token_start in range(0, len(tokens), step):
        token_end = min(token_start + MAX_TOKENS, len(tokens))
        chunk_text = _tokenizer.decode(tokens[token_start:token_end])
        # Approximate line numbers by counting newlines up to this token position
        prefix_text = _tokenizer.decode(tokens[:token_start])
        s_line = start_line + prefix_text.count("\n")
        e_line = s_line + chunk_text.count("\n")
        chunks.append(Chunk(
            content=chunk_text,
            file_path=file_path,
            start_line=s_line,
            end_line=e_line,
            language=language,
            chunk_index=idx,
        ))
        idx += 1
        if token_end == len(tokens):
            break

    return chunks


def _chunk_with_tree_sitter(source: str, language: str, file_path: str) -> list[Chunk]:
    try:
        parser = get_parser(language)
    except Exception:
        return []

    tree = parser.parse(source.encode())
    root = tree.root_node
    target_types = set(SEMANTIC_NODE_TYPES.get(language, []))
    chunks: list[Chunk] = []
    idx = 0

    def visit(node) -> None:
        nonlocal idx
        if target_types and node.type in target_types:
            text = source[node.start_byte:node.end_byte]
            start_line = node.start_point[0] + 1
            end_line = node.end_point[0] + 1
            if _token_count(text) <= MAX_TOKENS:
                chunks.append(Chunk(
                    content=text,
                    file_path=file_path,
                    start_line=start_line,
                    end_line=end_line,
                    language=language,
                    chunk_index=idx,
                ))
                idx += 1
            else:
                # Node too large — sub-chunk by tokens
                sub = _split_by_tokens(text, start_line, file_path, language, idx)
                chunks.extend(sub)
                idx += len(sub)
            return  # Don't recurse into already-captured node
        for child in node.children:
            visit(child)

    visit(root)

    # If tree-sitter found nothing (e.g. empty file or no semantic nodes), fall back
    if not chunks:
        return _split_by_tokens(source, 1, file_path, language, 0)

    return chunks


def chunk_file(file_path: str | Path) -> list[Chunk]:
    """Chunk a single file into semantic or token-window chunks."""
    path = Path(file_path)
    suffix = path.suffix.lower()
    language = LANGUAGE_MAP.get(suffix, "")

    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    if not source.strip():
        return []

    if language and _TS_AVAILABLE and SEMANTIC_NODE_TYPES.get(language) is not None:
        chunks = _chunk_with_tree_sitter(source, language, str(file_path))
        if chunks:
            return chunks

    # Plain text / unsupported extension → token-window fallback
    return _split_by_tokens(source, 1, str(file_path), language or "text", 0)


def chunk_text(text: str, file_path: str = "<inline>", language: str = "text") -> list[Chunk]:
    """Chunk arbitrary text (not from disk)."""
    return _split_by_tokens(text, 1, file_path, language, 0)
