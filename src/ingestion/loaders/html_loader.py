# ============================================================
# HTML Loader — Web Pages to Clean Text
#
# WHY NOT JUST READ THE HTML FILE AS TEXT?
# Raw HTML is full of noise: <script> tags, <style> blocks, navigation
# menus, footer links, cookie banners. Embedding this noise would
# pollute the semantic search — "Accept cookies" matching a query about
# data privacy, for example.
#
# The strategy here:
# 1. Parse with BeautifulSoup
# 2. Remove all script, style, nav, footer, header boilerplate
# 3. Extract text section by section, using headings (h1-h6) as
#    natural boundaries — similar to chapters in a book
# 4. Return one Document per section
#
# KNOWN LIMITATION:
# JavaScript-rendered content (React/Angular SPAs) won't appear in
# static HTML files — the div tags are empty until JS runs. This loader
# works only on static HTML. For JS-rendered pages, you'd need a
# headless browser (Selenium/Playwright) — out of scope for this project.
# ============================================================

import logging
import re
from pathlib import Path

from src.ingestion.document import Document

logger = logging.getLogger(__name__)

# HTML tags whose content should be completely discarded
_TAGS_TO_REMOVE = {
    "script", "style", "nav", "footer", "header",
    "aside", "noscript", "iframe", "form", "button",
}

# Heading tags that act as section boundaries
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def load_html(file_path: Path) -> list[Document]:
    """
    Load an HTML file and extract meaningful text content as Documents.

    Uses heading tags (h1-h6) as natural section separators. Each section
    between headings becomes a separate Document.

    Args:
        file_path: Path to the .html or .htm file.

    Returns:
        List of Documents — one per section/heading group.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise ImportError(
            "beautifulsoup4 is required for HTML loading. "
            "Install with: pip install beautifulsoup4"
        )

    try:
        # Read file with encoding detection
        try:
            raw_html = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raw_html = file_path.read_text(encoding="latin-1")

        soup = BeautifulSoup(raw_html, "html.parser")

        # Remove noisy tags entirely (including their content)
        for tag_name in _TAGS_TO_REMOVE:
            for tag in soup.find_all(tag_name):
                tag.decompose()

        # Extract the page title for context
        title = ""
        if soup.title and soup.title.string:
            title = soup.title.string.strip()

        # Find the main content area if it exists, otherwise use body
        main = (
            soup.find("main")
            or soup.find("article")
            or soup.find(id=re.compile(r"content|main|body", re.I))
            or soup.find("body")
            or soup
        )

        documents = _extract_sections(main, file_path, title)

        if not documents:
            # Fallback: just grab all text if section extraction found nothing
            all_text = _clean_text(main.get_text())
            if all_text:
                documents = [
                    Document(
                        text=all_text,
                        metadata={
                            "source": file_path.name,
                            "source_path": str(file_path),
                            "type": "html",
                            "title": title,
                            "section": "full_page",
                        },
                    )
                ]

        logger.info("Loaded %d sections from %s", len(documents), file_path.name)
        return documents

    except Exception as exc:
        logger.error("Failed to load HTML file %s: %s", file_path.name, exc)
        raise


def _extract_sections(soup_element, file_path: Path, page_title: str) -> list[Document]:
    """
    Walk the parsed HTML tree and group content by heading sections.

    Strategy:
    - Collect all elements in order
    - When a heading tag is hit, start a new section
    - Content between headings belongs to the preceding heading's section

    Returns:
        List of Documents, one per section.
    """
    documents: list[Document] = []
    current_heading = page_title or file_path.stem
    current_parts: list[str] = []

    def flush_section(heading: str, parts: list[str]) -> None:
        """Save accumulated text parts as a Document."""
        text = _clean_text("\n".join(parts))
        if text:
            documents.append(
                Document(
                    text=text,
                    metadata={
                        "source": file_path.name,
                        "source_path": str(file_path),
                        "type": "html",
                        "title": page_title,
                        "section": heading,
                    },
                )
            )

    for element in soup_element.descendants:
        tag_name = getattr(element, "name", None)

        if tag_name in _HEADING_TAGS:
            # New section starts — flush the previous one
            flush_section(current_heading, current_parts)
            current_heading = _clean_text(element.get_text())
            current_parts = []
        elif tag_name is None:
            # NavigableString (plain text node) — add to current section
            text = str(element).strip()
            if text:
                current_parts.append(text)
        elif tag_name == "p" or tag_name in ("li", "td", "th"):
            text = element.get_text(separator=" ", strip=True)
            if text:
                current_parts.append(text)

    # Flush the final section
    flush_section(current_heading, current_parts)

    return documents


def _clean_text(text: str) -> str:
    """Remove excessive whitespace and blank lines from extracted text."""
    # Replace multiple spaces with single space
    text = re.sub(r" +", " ", text)
    # Replace 3+ newlines with 2 (paragraph break)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
