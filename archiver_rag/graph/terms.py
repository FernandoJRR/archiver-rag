"""Term extraction for per-folder description generation.

§2.1 — Small folder (< term_extraction_min_notes): frontmatter tag frequency.
       Large folder: c-TF-IDF against all describable folders.
§2.2 — MMR to 4-6 diverse terms and 2-3 distinctive (by raw IDF weight).
§2.3 — Centroid embedding is in graph/centroids.py (Stage B).

§4 Adaptive α: alpha_for(note_count) controls how much the description drifts
with new content. Two additional meters (term_dispersion, embedding_compactness)
are computed and stored at weight 0.0 per §4.1 staggered rollout — they do not
affect α until activated by future config.

No I/O: every function takes vault: Path and returns plain data.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from pathlib import Path

import numpy as np

from archiver_rag.utils import extract_frontmatter, is_indexable_note, FOLDER_NOTE_NAME
from archiver_rag.vault.folder_notes import describable_folders

# ──────────────────────────────────────────────────────────────────────────────
# Stopwords (English + Spanish mix — vault uses both)
# ──────────────────────────────────────────────────────────────────────────────
_STOPWORDS = frozenset(
    {
        # English
        "the", "and", "for", "that", "this", "with", "from", "are", "was",
        "has", "have", "had", "not", "but", "can", "will", "all", "any",
        "its", "one", "two", "three", "into", "also", "each", "when", "then",
        "than", "more", "most", "some", "such", "only", "both", "very",
        "been", "being", "were", "they", "their", "there", "these", "those",
        "which", "what", "how", "where", "why", "who", "via", "per", "after",
        "before", "use", "used", "using", "uses", "call", "called", "calls",
        "add", "added", "adds", "new", "old", "set", "sets", "get", "gets",
        "run", "runs", "see", "note", "notes", "file", "files",
        # Spanish
        "que", "una", "las", "los", "del", "con", "por", "para", "como",
        "este", "esta", "esto", "cada", "pero", "hay", "son", "sus", "sin",
        "también", "cuando", "antes", "después", "nunca", "siempre", "solo",
        "desde", "hasta", "sobre", "entre", "porque", "aunque", "donde",
        "todo", "todos", "todas", "ser", "estar", "hace", "hacen", "tiene",
        "tienen", "puede", "pueden", "debe", "deben", "mismo", "misma",
        "mismos", "mismas", "bien", "mal", "así", "aquí", "ahora", "más",
        "menos", "muy", "ya", "aún", "sino", "pues",
        # Project-ubiquitous (would dominate everywhere, carry no folder signal)
        "archiver", "rag", "vault", "obsidian", "archiver-rag",
        # Common frontmatter field names that leak into body examples
        "type", "types", "source", "date", "tags", "related", "title",
        # Spanish variants that survive accent-stripping
        "descripcion", "configuracion", "implementacion", "solucion",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9-]{2,}")


def _normalize(text: str) -> str:
    """Lowercase and strip accents so 'descripción' → 'descripcion'."""
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _strip_related_section(content: str) -> str:
    """Remove the auto-generated ## Related block.

    Linker writes ~12 neighbour slugs per note. Counting them produces a list of
    other notes' filenames instead of terms that characterise the folder.
    """
    # Lazy import to avoid circular dependency at module level
    from archiver_rag.graph.linker import _find_related_section

    span = _find_related_section(content)
    if span is None:
        return content
    heading_start, _body_start, body_end = span
    return content[:heading_start] + content[body_end:]


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(_normalize(text)) if t not in _STOPWORDS]


def _note_tags(fm: dict) -> list[str]:
    raw = fm.get("tags", [])
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    return []


def _read_note_corpus(note_path: Path) -> tuple[list[str], list[str]]:
    """Return (tags, body_tokens) for a single note, with Related stripped."""
    try:
        raw = note_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return [], []
    fm, body = extract_frontmatter(raw)
    body = _strip_related_section(body)
    return _note_tags(fm), _tokenize(body)


def _folder_corpus(
    vault: Path, rel_folder: str
) -> tuple[list[str], list[str]]:
    """Return (all_tags, all_body_tokens) for direct notes in rel_folder."""
    folder = vault / rel_folder
    all_tags: list[str] = []
    all_tokens: list[str] = []
    for f in folder.iterdir():
        if f.is_file() and is_indexable_note(f):
            tags, tokens = _read_note_corpus(f)
            all_tags.extend(tags)
            all_tokens.extend(tokens)
    return all_tags, all_tokens


def _mmr(
    candidates: list[str],
    scores: dict[str, float],
    max_terms: int,
    mmr_lambda: float,
) -> list[str]:
    """Maximal Marginal Relevance over string terms using embed() for similarity.

    Each chosen term penalises semantically-similar candidates so the result
    covers distinct facets of the folder.
    """
    if not candidates:
        return []

    from archiver_rag.core.embedder import embed

    vecs = np.array(embed(candidates), dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vecs = vecs / norms

    selected_indices: list[int] = []
    remaining = list(range(len(candidates)))

    while remaining and len(selected_indices) < max_terms:
        if not selected_indices:
            # First pick: highest relevance score
            best = max(remaining, key=lambda i: scores.get(candidates[i], 0.0))
        else:
            sel_vecs = vecs[selected_indices]

            def mmr_score(i: int) -> float:
                rel = scores.get(candidates[i], 0.0)
                sim = float(np.max(sel_vecs @ vecs[i]))
                return mmr_lambda * rel - (1 - mmr_lambda) * sim

            best = max(remaining, key=mmr_score)

        selected_indices.append(best)
        remaining.remove(best)

    return [candidates[i] for i in selected_indices]


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def extract_terms(
    vault: Path,
    rel_folder: str,
    *,
    term_extraction_min_notes: int = 4,
    max_terms: int = 6,
    mmr_lambda: float = 0.5,
    tag_terms_in_description: bool = True,
) -> tuple[list[str], list[str]]:
    """Compute (description_terms, distinctive) for one folder.

    description_terms — 4–6 MMR-diversified terms.
    distinctive       — 2–3 terms with highest raw IDF (rarest across folders).

    Uses tag frequency for small folders, c-TF-IDF for larger ones.
    All described-folder context (for IDF) is computed once from describable_folders().
    """
    folder_dir = vault / rel_folder
    direct_notes = [f for f in folder_dir.iterdir() if f.is_file() and is_indexable_note(f)]
    n_notes = len(direct_notes)

    if n_notes == 0:
        return [], []

    if n_notes < term_extraction_min_notes:
        return _terms_by_tags(vault, rel_folder, max_terms, mmr_lambda)

    return _terms_by_ctfidf(
        vault, rel_folder, max_terms, mmr_lambda, tag_terms_in_description
    )


def extract_terms_all(
    vault: Path,
    *,
    term_extraction_min_notes: int = 4,
    max_terms: int = 6,
    mmr_lambda: float = 0.5,
    tag_terms_in_description: bool = True,
) -> dict[str, tuple[list[str], list[str]]]:
    """Extract terms for every describable folder in one pass.

    Pre-builds the cross-folder IDF table once so c-TF-IDF folders don't each
    recompute it independently.
    """
    folders = describable_folders(vault)
    corpora: dict[str, tuple[list[str], list[str]]] = {
        f: _folder_corpus(vault, f) for f in folders
    }

    result: dict[str, tuple[list[str], list[str]]] = {}
    for rel_folder in folders:
        folder_dir = vault / rel_folder
        direct_notes = [
            f for f in folder_dir.iterdir() if f.is_file() and is_indexable_note(f)
        ]
        if len(direct_notes) < term_extraction_min_notes:
            desc, dist = _terms_by_tags_corpus(
                corpora[rel_folder][0], max_terms, mmr_lambda
            )
        else:
            desc, dist = _terms_by_ctfidf_corpus(
                rel_folder, corpora, max_terms, mmr_lambda, tag_terms_in_description
            )
        result[rel_folder] = (desc, dist)
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Strategy implementations
# ──────────────────────────────────────────────────────────────────────────────

def _terms_by_tags(
    vault: Path, rel_folder: str, max_terms: int, mmr_lambda: float
) -> tuple[list[str], list[str]]:
    tags, _ = _folder_corpus(vault, rel_folder)
    return _terms_by_tags_corpus(tags, max_terms, mmr_lambda)


def _terms_by_tags_corpus(
    tags: list[str], max_terms: int, mmr_lambda: float
) -> tuple[list[str], list[str]]:
    counts = Counter(
        t
        for raw in tags
        if (t := _normalize(raw).replace(" ", "-").strip())
        and t not in _STOPWORDS
        and len(t) >= 3
    )
    if not counts:
        return [], []
    total = sum(counts.values())
    scores = {t: c / total for t, c in counts.items()}
    candidates = [t for t, _ in counts.most_common(25)]
    description_terms = _mmr(candidates, scores, max_terms, mmr_lambda)
    distinctive = [t for t, _ in counts.most_common(3) if t not in description_terms[:3]][:2]
    return description_terms, distinctive


def _terms_by_ctfidf(
    vault: Path,
    rel_folder: str,
    max_terms: int,
    mmr_lambda: float,
    tag_terms_in_description: bool = True,
) -> tuple[list[str], list[str]]:
    folders = describable_folders(vault)
    corpora = {f: _folder_corpus(vault, f) for f in folders}
    return _terms_by_ctfidf_corpus(
        rel_folder, corpora, max_terms, mmr_lambda, tag_terms_in_description
    )


def _terms_by_ctfidf_corpus(
    rel_folder: str,
    corpora: dict[str, tuple[list[str], list[str]]],
    max_terms: int,
    mmr_lambda: float,
    tag_terms_in_description: bool = True,
) -> tuple[list[str], list[str]]:
    """BERTopic-style c-TF-IDF: w = tf_{t,c} · log(1 + A / f_t).

    tf_{t,c}  — term frequency within the target folder's concatenated body.
    A          — mean token count across all folders.
    f_t        — total occurrences across all folders (document frequency by count).

    tag_terms_in_description=True (default): tags are normalized the same way
    _terms_by_tags_corpus does (accent-strip, lowercase, hyphen-join, stopword/length
    filter) and scored in their own tf pool, separate from body tokens. A folder-wide
    tag has real signal even when the folder's body is thousands of tokens — merging
    unnormalized tags straight into the body pool (the old behavior) diluted a tag's
    tf toward zero by sheer body volume, and fragmented the same tag written
    differently across notes ("Weekly-Cuisine" vs "weekly-cuisine") into separate
    terms. False reverts to that old single-pool, unnormalized behavior.
    """

    def _norm_tags(tags: list[str]) -> list[str]:
        return [
            t
            for raw in tags
            if (t := _normalize(raw).replace(" ", "-").strip())
            and t not in _STOPWORDS
            and len(t) >= 3
        ]

    def _prep(tags: list[str], tokens: list[str]) -> tuple[list[str], list[str]]:
        return (_norm_tags(tags) if tag_terms_in_description else tags, tokens)

    target_tags, target_tokens = _prep(*corpora.get(rel_folder, ([], [])))
    if not target_tags and not target_tokens:
        return [], []

    # Cross-folder totals — tags normalized the same way (when enabled) so document
    # frequency recognizes differently-cased/spaced tags as the same term.
    global_counts: Counter[str] = Counter()
    folder_sizes: list[int] = []
    for raw_tags, raw_tokens in corpora.values():
        tags, tokens = _prep(raw_tags, raw_tokens)
        combined = tags + tokens
        global_counts.update(combined)
        folder_sizes.append(len(combined))

    avg_size = sum(folder_sizes) / max(len(folder_sizes), 1)

    def _idf(term: str) -> float:
        return math.log(1 + avg_size / max(global_counts[term], 1))

    scores: dict[str, float] = {}
    if tag_terms_in_description:
        for pool in (target_tags, target_tokens):
            if not pool:
                continue
            counts = Counter(pool)
            total = sum(counts.values())
            for term, count in counts.items():
                score = (count / total) * _idf(term)
                # A term scoring in both pools (rare, but possible) keeps its
                # higher score instead of being overwritten by the weaker one.
                scores[term] = max(scores.get(term, 0.0), score)
    else:
        combined = target_tags + target_tokens
        counts = Counter(combined)
        total = sum(counts.values())
        for term, count in counts.items():
            scores[term] = (count / total) * _idf(term)

    # Top candidates for MMR
    top = sorted(scores, key=lambda t: scores[t], reverse=True)[:25]
    description_terms = _mmr(top, scores, max_terms, mmr_lambda)

    # Distinctive: highest raw IDF (rarest across folders), not MMR-diversified
    distinctive_candidates = sorted(scores, key=_idf, reverse=True)
    distinctive = [t for t in distinctive_candidates if t not in description_terms][:3]

    return description_terms, distinctive


# ──────────────────────────────────────────────────────────────────────────────
# §4 Adaptive α — maturity
# ──────────────────────────────────────────────────────────────────────────────

def alpha_for(note_count: int, *, scale: float = 1.0) -> float:
    """Logarithmic decay of description update strength.

    α = 1 / (1 + scale·log1p(note_count))

    At 0 notes: 1.0 (full replacement — folder identity not yet formed).
    At 4 notes: ~0.38.
    At 40 notes: ~0.21.
    Never reaches 0, so content retains power to redefine even mature folders.

    `scale` is the alpha_curve.scale config key (default 1.0).
    """
    import math
    return 1.0 / (1.0 + scale * math.log1p(max(0, note_count)))


def blend_terms(
    old: list[str],
    new: list[str],
    alpha: float,
    max_terms: int,
) -> list[str]:
    """Blend two ranked term lists using α as the weight for the new terms.

    desc_new = (1−α)·old + α·new — applied on rank scores, not strings.
    Each position i contributes score 1 − i/len to its term, so rank 0 is
    most valued. Scores are weighted (1−α) for old and α for new, summed per
    term, top max_terms kept. Deterministic, no embed() call.
    """
    if not old:
        return new[:max_terms]
    if not new:
        return old[:max_terms]

    combined: dict[str, float] = {}
    for i, t in enumerate(old):
        combined[t] = combined.get(t, 0.0) + (1 - alpha) * (1 - i / len(old))
    for i, t in enumerate(new):
        combined[t] = combined.get(t, 0.0) + alpha * (1 - i / len(new))

    return [t for t, _ in sorted(combined.items(), key=lambda kv: kv[1], reverse=True)[:max_terms]]


def term_dispersion(tokens: list[str]) -> float:
    """Normalized entropy of the folder's token distribution.

    0 = every token is the same term (highly focused).
    1 = uniform distribution across many terms (scattered).

    §4.1: computed but weighted 0.0 at launch — stored in _folder.md for observation.
    """
    import math
    if not tokens:
        return 0.0
    total = len(tokens)
    freq = Counter(tokens)
    entropy = -sum((c / total) * math.log2(c / total) for c in freq.values())
    max_entropy = math.log2(len(freq)) if len(freq) > 1 else 1.0
    return entropy / max_entropy


def embedding_compactness(vault: Path, rel_folder: str, centroid) -> float:
    """Mean cosine of each direct note to the folder's centroid.

    1 = every note is identical to the centroid (maximally compact).
    0 = notes scatter uniformly.

    §4.1: computed but weighted 0.0 at launch. `centroid` is a pre-loaded unit vector
    from graph.centroids so this function doesn't touch the cache.
    """
    import numpy as np

    folder = vault / rel_folder
    paths = [f for f in folder.iterdir() if f.is_file() and is_indexable_note(f)]
    if not paths:
        return 0.0

    from archiver_rag.core.embedder import embed
    from archiver_rag.graph.centroids import _unit, cosine

    texts = []
    for p in paths:
        try:
            texts.append(p.read_text(encoding="utf-8", errors="ignore")[:2000])
        except Exception:
            texts.append("")

    if not texts:
        return 0.0

    vecs = [_unit(v) for v in embed(texts)]
    c_unit = _unit(centroid) if not isinstance(centroid, np.ndarray) else centroid
    sims = [cosine(v, c_unit) for v in vecs]
    return float(sum(sims) / len(sims))
