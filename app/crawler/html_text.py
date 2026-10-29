"""HTML -> readable text, title, description and links, with the standard-library parser (no extra dependency).

Navigation, headers, footers, scripts, styles, forms and hidden elements are dropped, so the text is mostly the page's
own content. Block elements become line breaks; headings are kept as lines starting with '#', which helps extraction.
"""

import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser

DROP = {"script", "style", "noscript", "template", "svg", "canvas", "iframe", "nav", "header", "footer", "form",
        "button", "select", "option", "aside", "head"}  # fmt: skip
VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
BLOCK = {"p", "div", "section", "article", "main", "li", "ul", "ol", "table", "tr", "td", "th", "br", "dd", "dt",
         "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "figcaption", "caption", "address"}  # fmt: skip
HEADINGS = {"h1", "h2", "h3", "h4"}
# Hidden or visually suppressed text (a classic way to plant instructions for AI readers).
HIDDEN = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?![.\d])|opacity\s*:\s*0(?![.\d])|"
    r"(?:left|top|text-indent)\s*:\s*-\d{3,}|clip\s*:\s*rect\(\s*0|(?:height|width)\s*:\s*[01]px",
    re.I,
)
HIDDEN_CLASSES = {"sr-only", "visually-hidden", "screen-reader-text", "hidden", "d-none", "invisible"}
BOILERPLATE = re.compile(r"cookie|consent|gdpr|newsletter|subscribe|breadcrumb|share-|social", re.I)


@dataclass
class HtmlDoc:
    title: str = ""
    description: str = ""
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)  # (href, anchor text)
    language: str = ""


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.doc = HtmlDoc()
        self._skip_depth = 0  # >0 while inside a dropped element
        self._stack: list[tuple[str, bool]] = []  # (tag, opened a dropped region)
        self._chunks: list[str] = []
        self._in_title = False
        self._link: tuple[str, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "html" and a.get("lang"):
            self.doc.language = a["lang"][:12]
        if tag == "meta" and a.get("name", "").lower() == "description" and not self.doc.description:
            self.doc.description = unescape(a.get("content", "")).strip()[:500]
        if tag == "title":
            self._in_title = True
        if tag in VOID:
            if tag == "br" and not self._skip_depth:
                self._chunks.append("\n")
            return
        if tag == "a" and a.get("href"):
            # Links are collected everywhere, menus included (that's where product pages are linked from),
            # even though menu text is kept out of the page text.
            self._link = (a["href"], [])
        drops = (
            tag in DROP
            or "hidden" in a
            or a.get("aria-hidden") == "true"
            or bool(HIDDEN.search(a.get("style", "")))
            or not HIDDEN_CLASSES.isdisjoint(a.get("class", "").lower().split())
            or bool(BOILERPLATE.search(f"{a.get('id', '')} {a.get('class', '')}"))
        )
        if tag == "head":
            drops = True
        self._stack.append((tag, drops))
        if drops:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in BLOCK:
            self._chunks.append("\n")
        if tag in HEADINGS:
            self._chunks.append("# ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in VOID:
            return
        if tag == "a" and self._link:
            href, words = self._link
            self.doc.links.append((href, " ".join(" ".join(words).split())[:200]))
            self._link = None
        # Close up to the matching tag (HTML in the wild is often unbalanced).
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                for _, dropped in self._stack[i:]:
                    if dropped:
                        self._skip_depth -= 1
                del self._stack[i:]
                break
        if self._skip_depth:
            return
        if tag in BLOCK:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.doc.title += data
            return
        if self._link:
            self._link[1].append(data)
        if self._skip_depth:
            return
        self._chunks.append(data)

    def result(self) -> HtmlDoc:
        lines = []
        for line in "".join(self._chunks).splitlines():
            line = " ".join(line.split())
            if line and line != "#":
                lines.append(line)
        # Collapse repeated lines (menus rendered twice, repeated taglines).
        seen: set[str] = set()
        unique = [ln for ln in lines if not (len(ln) < 120 and ln in seen) and not seen.add(ln)]  # type: ignore[func-returns-value]
        self.doc.text = "\n".join(unique)
        self.doc.title = " ".join(self.doc.title.split())[:300]
        return self.doc


def html_to_text(html: str) -> HtmlDoc:
    parser = _Parser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: S110 - a broken page still yields whatever parsed before the error
        pass
    return parser.result()
