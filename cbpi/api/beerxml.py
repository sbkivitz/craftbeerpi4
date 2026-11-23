"""Reading an uploaded BeerXML file whatever its encoding, and comparing recipe names.

An upload's bytes don't always fit the encoding it declares: BeerSmith declares ISO-8859-1 and may write Windows-1252
quotes and dashes (0x92, 0x96); an editor may save UTF-8 under that declaration, or Latin-1 under a UTF-8 one, add a
byte-order mark, or leave NUL bytes and other control characters XML forbids. None of that may lose a recipe:

- read() tries, in order: the encoding the file declares (UTF-8 when it declares none), UTF-8, Windows-1252 and
  Latin-1, which reads any bytes. The first that decodes and parses as XML is the one. A UTF-8 byte-order mark is
  dropped first.
- When none of them parses, the text the first of them decoded is made plain (plain()): what XML forbids is dropped,
  and every character outside plain ASCII becomes its plain English letters or goes. Then it is parsed again.

Recipe names are compared by name_key(), so "Kolsch" and "Kölsch" are the same recipe. Plugins reading the Recipe
Book's BeerXML files (RecipeController.SOURCE_KEYS) do the same; recipe-source-xml.md gives them this order and
these functions.
"""

import codecs
import re
import unicodedata
import xml.etree.ElementTree as ElementTree
from collections import namedtuple

UTF8_BOM = codecs.BOM_UTF8
#: An XML declaration's encoding, at the very start of the bytes.
DECLARED = re.compile(rb"""\A\s*<\?xml\s[^>]*?\bencoding\s*=\s*["']([A-Za-z][A-Za-z0-9._-]*)["']""")
#: After the declared encoding, in this order.
FALLBACKS = ("utf-8", "cp1252", "latin-1")

#: read()'s answer: the parsed document, its text, the encoding it was read in, and whether it had to be made plain.
Read = namedtuple("Read", "root text encoding plain")

#: Characters plain() writes as other plain English letters. The rest go through NFKD with what isn't plain ASCII
#: dropped: é is e, ö is o, the ﬁ ligature fi, a no-break space a space.
PLAIN = {
    "ß": "ss", "ẞ": "SS", "Æ": "AE", "æ": "ae", "Œ": "OE", "œ": "oe", "Ø": "O", "ø": "o",
    "Ð": "D", "ð": "d", "Đ": "D", "đ": "d", "Þ": "Th", "þ": "th", "Ł": "L", "ł": "l", "ı": "i",
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-",
    "⁄": "/", "°": "deg", "×": "x", "•": "*",
}


def declared_encoding(content):
    """The encoding the XML declaration at the start of `content` names, or None."""
    found = DECLARED.match(content[len(UTF8_BOM):] if content.startswith(UTF8_BOM) else content)
    return found.group(1).decode("ascii") if found else None


def decode_order(content):
    """The encodings read() tries for `content`, in order: the declared one (UTF-8 when there is none), then
    FALLBACKS, each once, leaving out a declared one Python doesn't know."""
    order = []
    for name in (declared_encoding(content) or "utf-8",) + FALLBACKS:
        try:
            codec = codecs.lookup(name).name
        except LookupError:
            continue
        if codec not in order:
            order.append(codec)
    return order


def plain(text):
    """`text` in plain ASCII, as the last way to read a file that won't parse.

    Character by character: a C1 control character (U+0080 to U+009F, which Latin-1 gives for the bytes 0x80 to
    0x9F) is first taken as the Windows-1252 character of that byte - 0x92 the quote ’, 0x96 the dash – - and is
    dropped where Windows-1252 has none (0x81, 0x8D, 0x8F, 0x90, 0x9D). Then plain ASCII (space to ~), tab, line
    feed and carriage return stay; a character in PLAIN becomes its letters (ß ss, ’ ', – -, ° deg); any other
    is decomposed (NFKD), keeping the plain ASCII of it and PLAIN's letters for the rest. What is left - control
    characters XML forbids, NUL among them, the accent marks split from their letters, and letters of other
    alphabets - is dropped.
    """
    out = []
    for ch in text:
        if "\x80" <= ch <= "\x9f":
            try:
                ch = bytes([ord(ch)]).decode("cp1252")
            except UnicodeDecodeError:
                continue
        if ch in "\t\n\r" or " " <= ch <= "~":
            out.append(ch)
        elif ch in PLAIN:
            out.append(PLAIN[ch])
        else:
            out.append("".join(c if " " <= c <= "~" else PLAIN.get(c, "")
                               for c in unicodedata.normalize("NFKD", ch)))
    return "".join(out)


def read(content):
    """Read the BeerXML bytes `content`. -> Read. Raises ElementTree.ParseError when even the plain text isn't XML.

    The first encoding in decode_order() that decodes and parses. When none parses, the text the first of them
    decoded, made plain (plain()) and parsed again.
    """
    if content.startswith(UTF8_BOM):
        content = content[len(UTF8_BOM):]
    first = None
    for encoding in decode_order(content):
        try:
            text = content.decode(encoding)
        except UnicodeDecodeError:
            continue
        if first is None:
            first = (text, encoding)
        try:
            return Read(ElementTree.fromstring(text), text, encoding, False)
        except ElementTree.ParseError:
            continue
    text, encoding = first
    text = plain(text)
    return Read(ElementTree.fromstring(text), text, encoding, True)


def utf8_document(text):
    """The document `text` as UTF-8 bytes, under a UTF-8 declaration in place of its own."""
    body = re.sub(r"\A\s*<\?xml\s[^>]*\?>", "", text, count=1).lstrip("\ufeff")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + body.lstrip()).encode("utf-8")


def name_key(name):
    """A recipe name as names are compared: NFKD, combining marks dropped, casefolded, runs of whitespace one
    space, none at the ends. "Kölsch", "Kolsch" and " kolsch " are the same; anything not text is ""."""
    if not isinstance(name, str):
        return ""
    unmarked = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
    return " ".join(unmarked.casefold().split())
