"""Sanitising untrusted text before it reaches the AI (prompt-injection defence in depth).

The main defences are structural: the AI can only *propose* (curators approve), its output is a strict schema with no
status or permission fields, untrusted text is fenced as data, and every fact must quote text that really is on a
fetched page. This module adds a cheap filter in front:

- invisible characters that hide or reorder text (zero-width, bidi overrides) are removed;
- control characters are removed, whitespace collapsed;
- lines that read like instructions to an AI are replaced with a marker and counted, so the page can be flagged.

Because evidence is checked against the *sanitised* text, a fact can't be "proven" by quoting a redacted line.
"""

import re
from dataclasses import dataclass

INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿­]")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
REDACTED = "[removed: instruction-like text]"

INJECTION = re.compile(
    r"""
    \b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|any|the|your)\b[^.\n]{0,30}
        \b(instruction|instructions|prompt|prompts|rules|directions|guidelines|context)\b
    | \byou\s+are\s+now\b
    | \b(system|developer)\s+(prompt|message|instruction)s?\b
    | \bnew\s+instructions?\s*:
    | ^\s*(system|assistant|user)\s*:
    | <\s*/?\s*(system|assistant|instructions?)\s*>
    | \b(mark|set|flag|record)\b[^.\n]{0,40}\b(as\s+)?(approved|verified|trusted|whitelisted)\b
    | \b(approve|auto[- ]?approve)\s+(this|the)\s+(vendor|supplier|company|proposal)\b
    | \bas\s+an?\s+(ai|language\s+model|llm|assistant)\b
    | \b(output|return|respond\s+with)\b[^.\n]{0,30}\b(json|the\s+following)\b
    """,
    re.IGNORECASE | re.VERBOSE | re.MULTILINE,
)


@dataclass
class Sanitised:
    text: str
    redacted_lines: int

    @property
    def suspicious(self) -> bool:
        return self.redacted_lines > 0


def sanitise(text: str) -> Sanitised:
    text = CONTROL.sub(" ", INVISIBLE.sub("", text))
    lines: list[str] = []
    redacted = 0
    for line in text.splitlines():
        if INJECTION.search(line):
            if not lines or lines[-1] != REDACTED:
                lines.append(REDACTED)
            redacted += 1
            continue
        lines.append(re.sub(r"[ \t]{2,}", " ", line).strip())
    return Sanitised("\n".join(ln for ln in lines if ln), redacted)
