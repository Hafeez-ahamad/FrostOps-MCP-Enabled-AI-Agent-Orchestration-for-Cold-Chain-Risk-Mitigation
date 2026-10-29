"""SOP clause retrieval behind one interface.

The SOP is three short clauses, so the default backend keeps it in memory and ranks clauses
by keyword overlap. It is deterministic, offline and needs no embedding model or API key.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from engine.settings import SOP_PATH, Settings

_SECTION_HEADING = re.compile(r"^###\s+(\d+)\.\s+(.+?)\s*$", re.MULTILINE)
_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "the and for with are any all must not that this what when from into than does".split()
)


@dataclass(frozen=True)
class SopClause:
    clause_id: str  # e.g. "§1"
    title: str
    text: str
    source_file: str


class SopRetriever(Protocol):
    def search(self, query: str, k: int = 2) -> list[SopClause]: ...


def parse_sop_clauses(markdown: str, source_file: str) -> list[SopClause]:
    """Split the SOP into its numbered `### N. Title` sections."""
    headings = list(_SECTION_HEADING.finditer(markdown))
    clauses = []
    for i, match in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(markdown)
        clauses.append(
            SopClause(
                clause_id=f"§{match.group(1)}",
                title=match.group(2),
                text=markdown[match.end() : end].strip(),
                source_file=source_file,
            )
        )
    return clauses


def _keywords(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) >= 3 and w not in _STOPWORDS}


class LocalSopRetriever:
    """In-memory keyword-overlap ranking. Ties keep SOP order."""

    def __init__(self, sop_path: Path = SOP_PATH) -> None:
        markdown = sop_path.read_text(encoding="utf-8")
        self.clauses = parse_sop_clauses(markdown, source_file=sop_path.name)

    def search(self, query: str, k: int = 2) -> list[SopClause]:
        wanted = _keywords(query)
        scored = []
        for order, clause in enumerate(self.clauses):
            score = len(wanted & _keywords(clause.title + " " + clause.text))
            if score > 0:
                scored.append((-score, order, clause))
        scored.sort(key=lambda item: (item[0], item[1]))
        return [clause for _, _, clause in scored[:k]]


def build_sop_retriever(settings: Settings) -> SopRetriever:
    if settings.sop_backend == "LOCAL":
        return LocalSopRetriever()
    # Pinecone is an optional hosted backend. It is not wired up yet because it cannot be
    # tested offline; see docs/decisions.md.
    raise NotImplementedError("SOP_BACKEND=PINECONE is not available yet; use SOP_BACKEND=LOCAL")
