import re
from dataclasses import dataclass

from app.config import settings
from app.parsing import Block


@dataclass
class Chunk:
    title: str
    content: str
    locator: str


def chunk_blocks(blocks):
    target = settings.CHUNK_TARGET_CHARS
    overlap = settings.CHUNK_OVERLAP_CHARS
    if not 0 <= overlap < target:
        raise ValueError("Invalid chunk target/overlap")
    output = []
    pending = []
    current_section = None

    def emit():
        if pending:
            output.append(Chunk(current_section, "\n".join(t for t, _ in pending),
                                ",".join(dict.fromkeys(loc for _, loc in pending))))

    for block in blocks:
        # Keep normal paragraphs intact; only oversized paragraphs need sentence splitting.
        parts = [block.text] if len(block.text) <= target * 2 else re.split(r"(?<=[。！？；])", block.text)
        pieces = []
        for part in parts:
            if len(part) <= target * 2:
                if part.strip():
                    pieces.append(part.strip())
            else:
                start = 0
                while start < len(part):
                    pieces.append(part[start:start + target])
                    if start + target >= len(part):
                        break
                    start += target - overlap
        for piece in pieces:
            if current_section != block.section:
                emit()
                pending = []
                current_section = block.section
            length = sum(len(t) + 1 for t, _ in pending)
            if pending and length + len(piece) > target:
                emit()
                tail = pending[-1:]
                pending = tail if len(tail[0][0]) <= overlap else []
            pending.append((piece, block.locator))
    emit()
    return output


def split_document(text):
    blocks = [Block(p, "正文", f"block:{i}") for i, p in enumerate(text.split("\n\n"), 1) if p.strip()]
    return [(c.title, c.content) for c in chunk_blocks(blocks)]
