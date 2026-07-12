"""OCR text cleaner for scanned PDFs.

Removes running headers, page numbers, form feeds, broken line wraps,
and common OCR artifacts. Reconstructs paragraphs and detects section/chapter
structure for cleaner ebook output.
"""

import re


def clean_ocr_text(text: str) -> str:
    """Main entry point: clean a blob of raw OCR text line by line."""
    lines = text.split("\n")

    # ── Pass 1: remove garbage lines ──
    cleaned = []
    for line in lines:
        s = line.replace("\f", "").strip()

        # Running headers:  "NN A LYCANTHROPY READER"
        if re.match(r"^\d*\s*A LYCANTHROPY READER\s*$", s):
            continue
        # Standalone page numbers
        if re.match(r"^\d{1,3}$", s):
            continue
        # Running-head footers
        if re.match(
            r"^(INTRODUCTION|CONTENTS|CONTRIBUTORS|BIBLIOGRAPHY|INDEX|"
            r"Preface|Acknowledgments|Medical Descriptions|Medical Cases|"
            r"Trial Records|Historical Accounts|Anthropology|History|"
            r"Medicine|Allegory|Myths and Legends)\s+[ivxlcdm\d]+\s*$",
            s,
            re.IGNORECASE,
        ):
            continue
        # Roman-numeral page numbers
        if re.match(r"^[ivxlcdm]+$", s, re.IGNORECASE) and len(s) < 8:
            continue

        cleaned.append(s)

    text = "\n".join(cleaned)

    # ── Pass 2: fix OCR character errors ──
    text = re.sub(r"\bJames \|([,.\s])", r"James I\1", text)
    text = re.sub(r"\bElizabeth \|([,.\s])", r"Elizabeth I\1", text)
    text = re.sub(r"\bHenry \|I\b", "Henry II", text)
    # Standalone pipe after word → likely Roman numeral I
    text = re.sub(r"(?<=\w) \|(?![|A-Za-z])", " I", text)
    text = re.sub(r"\b\|(?=[IVXLCDM])", "I", text)  # pipe → I in Roman nums
    # SECTION | → SECTION I
    text = re.sub(r"\bSECTION \|", "SECTION I", text)
    text = text.replace("·", '"')  # middle-dot quotes → double quotes
    text = re.sub(r"\bnc\b", "B.C.", text)
    text = re.sub(r"\baD\b", "A.D.", text)
    text = re.sub(r"\bAD\b", "A.D.", text)
    text = text.replace("[ am", "I am")

    # ── Pass 3: join hyphenated line breaks ──
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)

    # ── Pass 4: reconstruct paragraphs ──
    # Split on blank lines → each block is a potential paragraph
    blocks = re.split(r"\n\s*\n", text)
    output = []

    for block in blocks:
        s = block.strip()
        if not s:
            continue

        # Join all lines inside the block
        s = re.sub(r"\s*\n\s*", " ", s)
        s = re.sub(r"  +", " ", s)

        # Decide whether this block is a heading
        first_word = s.split()[0] if s.split() else ""
        is_heading = bool(
            re.match(
                r"^(SECTION |Chapter |CHAPTER |\d+\.\s+|INTRODUCTION|"
                r"Preface|Acknowledgments|Illustrations?|CONTENTS|"
                r"CONTRIBUTORS|BIBLIOGRAPHY|INDEX|APPENDIX|NOTES?\b)",
                first_word,
                re.IGNORECASE,
            )
        )

        if is_heading or len(s) < 60:
            output.append(s.strip())
            output.append("")
        else:
            # Clean up spacing around punctuation
            s = re.sub(r"\s+([,.;:!?)])", r"\1", s)
            s = re.sub(r"([(])\s+", r"\1", s)
            s = re.sub(r"\s+\)", ")", s)
            s = re.sub(r'\s+"', '"', s)
            s = re.sub(r'"\s+', '"', s)
            output.append(s)
            output.append("")

    while output and not output[-1].strip():
        output.pop()

    return "\n".join(output)


def extract_structure(text: str):
    """Split cleaned text into chapter-like sections for ebook chapters.

    Returns a list of dicts:  {"heading": str, "body": str}
    """
    lines = text.split("\n")
    chapters = []
    current_heading = "Start"
    current_body = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Detect section/chapter headings
        m = re.match(
            r"^(Chapter |CHAPTER |SECTION )\s*(.*)", stripped, re.IGNORECASE
        )
        if m:
            # Flush previous chapter
            if current_body:
                chapters.append(
                    {"heading": current_heading,
                     "body": "\n\n".join(current_body)}
                )

            prefix = m.group(1).strip()
            rest = m.group(2).strip()

            # If heading swallowed body text (e.g. "Chapter 6 Introduction The first...")
            # try to split off a short intro word
            body_split = re.match(
                r"(\d+\s+\w+)\s+(.*)", rest
            )
            if body_split:
                current_heading = f"{prefix} {body_split.group(1)}"
                current_body = [body_split.group(2)]
            else:
                current_heading = f"{prefix} {rest}"
                current_body = []
        else:
            current_body.append(stripped)

    if current_body:
        chapters.append(
            {"heading": current_heading, "body": "\n\n".join(current_body)}
        )

    return chapters
