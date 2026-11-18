"""
Core DocuBot class responsible for:
- Loading documents from the docs/ folder
- Building a simple retrieval index (Phase 1)
- Retrieving relevant snippets (Phase 1)
- Supporting retrieval only answers
- Supporting RAG answers when paired with Gemini (Phase 2)
"""

import os
import glob
import re

# Words that show up in almost every question and say nothing about the topic.
# If we counted them, "Where is the token?" would match every file that has "is" or "the".
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for",
    "from", "how", "i", "if", "in", "is", "it", "of", "on", "or", "the", "this",
    "to", "what", "when", "where", "which", "who", "why", "with", "there", "any",
    "these", "that", "my", "me", "we", "you", "your", "all",
}

# Guardrail: a section only counts as evidence if it shares at least this many
# meaningful words with the question. One shared word is usually a coincidence
# (for example "docs" or "api"), so we ask for two.
MIN_MATCHING_TERMS = 2


def normalize(word):
    """
    Very small stemmer so different forms of a word match:
    "users" -> "user", "generated"/"generation" -> "generat", "connection" -> "connect".
    """
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            break
    if word.endswith("ion") and len(word) > 6:
        word = word[:-3]
    if word.endswith("e") and len(word) > 3:
        word = word[:-1]
    return word


def tokenize(text):
    """Lowercase the text, split it into plain words, and normalize each word."""
    return [normalize(w) for w in re.findall(r"[a-z0-9]+", text.lower())]


def query_terms(query):
    """The meaningful words in a question: stopwords removed, normalized, no repeats."""
    words = re.findall(r"[a-z0-9]+", query.lower())
    return {normalize(w) for w in words if w not in STOPWORDS}


def split_into_sections(text):
    """
    Cut one markdown file into sections, starting a new section at every heading.
    A heading with nothing under it (like "## User Data Endpoints") is glued onto
    the next section so it is never returned on its own.
    """
    sections = []
    current = []
    for line in text.splitlines():
        if line.startswith("#") and any(l.strip() and not l.startswith("#") for l in current):
            sections.append("\n".join(current).strip())
            current = []
        current.append(line)
    if any(l.strip() for l in current):
        sections.append("\n".join(current).strip())
    return sections


class DocuBot:
    def __init__(self, docs_folder="docs", llm_client=None):
        """
        docs_folder: directory containing project documentation files
        llm_client: optional Gemini client for LLM based answers
        """
        self.docs_folder = docs_folder
        self.llm_client = llm_client

        # Load documents into memory
        self.documents = self.load_documents()  # List of (filename, text)

        # Retrieval works on sections, not whole files. Each chunk is (filename, section_text).
        self.chunks = [
            (filename, section)
            for filename, text in self.documents
            for section in split_into_sections(text)
        ]

        # Inverted index: word -> positions in self.chunks that contain it
        self.index = self.build_index(self.chunks)

    # -----------------------------------------------------------
    # Document Loading
    # -----------------------------------------------------------

    def load_documents(self):
        """
        Loads all .md and .txt files inside docs_folder.
        Returns a list of tuples: (filename, text)
        """
        docs = []
        pattern = os.path.join(self.docs_folder, "*.*")
        for path in sorted(glob.glob(pattern)):
            if path.endswith(".md") or path.endswith(".txt"):
                with open(path, "r", encoding="utf8") as f:
                    text = f.read()
                filename = os.path.basename(path)
                docs.append((filename, text))
        return docs

    # -----------------------------------------------------------
    # Index Construction
    # -----------------------------------------------------------

    def build_index(self, documents):
        """
        Build an inverted index: each word maps to the positions (in `documents`)
        of every piece of text that contains it.

        Example: {"token": [3, 4, 9], "databas": [14, 15]}
        """
        index = {}
        for position, (_, text) in enumerate(documents):
            for word in set(tokenize(text)):
                index.setdefault(word, []).append(position)
        return index

    # -----------------------------------------------------------
    # Scoring and Retrieval
    # -----------------------------------------------------------

    def score_document(self, query, text):
        """
        Score = how many different meaningful question words appear in the text.
        Each word counts once, so a long section that repeats "token" ten times
        does not beat a short section that matches more of the question.
        """
        words_in_text = set(tokenize(text))
        return sum(1 for term in query_terms(query) if term in words_in_text)

    def retrieve(self, query, top_k=3):
        """
        Return up to top_k (filename, section_text) pairs, best match first.
        Sections that match fewer than MIN_MATCHING_TERMS question words are dropped,
        so an empty list means "the docs do not cover this".
        """
        terms = query_terms(query)
        needed = min(MIN_MATCHING_TERMS, len(terms))
        if needed == 0:
            return []

        # Only look at sections that share at least one word with the question.
        candidates = set()
        for term in terms:
            candidates.update(self.index.get(term, []))

        scored = []
        for position in candidates:
            filename, text = self.chunks[position]
            score = self.score_document(query, text)
            if score >= needed:
                # Tie-breaks: first the section that mentions the question words
                # more often, then the shorter (more focused) section.
                words = tokenize(text)
                mentions = sum(1 for w in words if w in terms)
                scored.append((score, mentions, -len(text), position))

        scored.sort(reverse=True)
        return [self.chunks[position] for *_, position in scored[:top_k]]

    # -----------------------------------------------------------
    # Answering Modes
    # -----------------------------------------------------------

    def answer_retrieval_only(self, query, top_k=3):
        """
        Phase 1 retrieval only mode.
        Returns raw snippets and filenames with no LLM involved.
        """
        snippets = self.retrieve(query, top_k=top_k)

        if not snippets:
            return "I do not know based on the docs I have."

        formatted = []
        for filename, text in snippets:
            formatted.append(f"[{filename}]\n{text}\n")

        return "\n---\n".join(formatted)

    def answer_rag(self, query, top_k=3):
        """
        Phase 2 RAG mode.
        Uses student retrieval to select snippets, then asks Gemini
        to generate an answer using only those snippets.
        """
        if self.llm_client is None:
            raise RuntimeError(
                "RAG mode requires an LLM client. Provide a GeminiClient instance."
            )

        snippets = self.retrieve(query, top_k=top_k)

        if not snippets:
            return "I do not know based on the docs I have."

        return self.llm_client.answer_from_snippets(query, snippets)

    # -----------------------------------------------------------
    # Bonus Helper: concatenated docs for naive generation mode
    # -----------------------------------------------------------

    def full_corpus_text(self):
        """
        Returns all documents concatenated into a single string.
        This is used in Phase 0 for naive 'generation only' baselines.
        """
        return "\n\n".join(text for _, text in self.documents)
