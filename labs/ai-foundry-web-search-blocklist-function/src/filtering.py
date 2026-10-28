"""Replace complete URLs on blocked domains with the literal [BLOCKED LINK]."""

import html
import re

REPLACEMENT = "[BLOCKED LINK]"


class InvalidRequest(ValueError):
    pass


def normalize_domain(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise InvalidRequest("Blocked domains must be nonempty hostnames")
    try:
        domain = value.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise InvalidRequest("Blocked domains must be valid hostnames") from exc
    if (len(domain) > 253 or "." not in domain
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                   for label in domain.split("."))):
        raise InvalidRequest("Blocked domains must be hostnames without schemes, ports, or paths")
    return domain


def domain_list(value):
    if not isinstance(value, list) or len(value) > 100:
        raise InvalidRequest("blocked_domains must be an array of at most 100 hostnames")
    return list(dict.fromkeys(normalize_domain(item) for item in value))


_URL = re.compile(
    r"(?i)(?<![\w@])(?:[a-z][a-z0-9+.-]*://[^\s<>\[\]\"'`]+"
    r"|//[^\s<>\[\]\"'`]+|www\.[^\s<>\[\]\"'`]+"
    r"|(?:[\w-]+\.)+[\w-]+(?::\d+)?/[^\s<>\[\]\"'`]*)"
)
_INLINE = re.compile(
    r"!?\[[^\]\n]*\]\(\s*(?P<target><[^>\n]+>|(?:[^\s()]|\([^()]*\))+)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)"
)
_REFERENCE = re.compile(r"(?m)^ {0,3}\[[^]\n]+\]:[ \t]*(?P<target><[^>\n]+>|\S+)")
_ATTRIBUTE = re.compile(r"\b(?:href|src)\s*=\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)'|(?P<bare>[^\s>]+))", re.I)
_ENTITY_OR_ESCAPE = re.compile(r"&(?:#\d+|#x[\da-fA-F]+|[a-zA-Z][a-zA-Z\d]+);|\\[\W_]", re.I)
_PERCENT_RUN = re.compile(r"(?:%[\da-fA-F]{2})+")
_DOTS = str.maketrans({"。": ".", "．": ".", "｡": "."})


def _decode(value, ends, pattern, decoder):
    """Retain original character boundaries while decoding a URL for matching."""
    characters, positions, start = [], [], 0
    for match in pattern.finditer(value):
        characters.append(value[start:match.start()])
        positions.extend(ends[start:match.start()])
        decoded, offsets = decoder(match[0])
        characters.append(decoded)
        positions.extend(ends[match.start() + offset - 1] for offset in offsets)
        start = match.end()
    characters.append(value[start:])
    positions.extend(ends[start:])
    return "".join(characters), positions


def _entity(value):
    decoded = value[1:] if value.startswith("\\") else html.unescape(value)
    return decoded, [len(value)] * len(decoded)


def _percent(value):
    try:
        decoded = bytes.fromhex(value.replace("%", "")).decode("utf-8")
    except (ValueError, UnicodeError):
        return value, list(range(1, len(value) + 1))
    position, offsets = 0, []
    for character in decoded:
        position += 3 * len(character.encode("utf-8"))
        offsets.append(position)
    return decoded, offsets


class DomainRedactor:
    def __init__(self, domains):
        domains = domain_list(domains)
        # A hostname boundary and end anchor prevent partial-domain/path matches.
        alternatives = "|".join(re.escape(domain) for domain in sorted(domains, key=len, reverse=True))
        self.pattern = re.compile(r"(?:^|\.)(?P<blocked>" + (alternatives or r"(?!)") + r")$", re.I)

    def replacement_span(self, target):
        """Return the complete URL span when its hostname is blocked."""
        decoded, ends = _decode(target, list(range(1, len(target) + 1)), _ENTITY_OR_ESCAPE, _entity)
        span = (0, len(target))
        if decoded.startswith("<") and decoded.endswith(">"):
            span = (ends[0], ends[-2])
            decoded, ends = decoded[1:-1], ends[1:-1]
        authority = re.match(r"(?i)^(?:(?:[a-z][a-z0-9+.-]*:)?//)?(?P<authority>[^/?#\\\s]+)", decoded)
        if not authority:
            return None
        start, end = authority.span("authority")
        # Match only the hostname, even though the entire URL will be replaced.
        start += decoded[start:end].rfind("@") + 1
        raw_host = decoded[start:end]
        if raw_host.startswith("["):
            return None  # DNS blocklists do not contain IP literals.
        if ":" in raw_host:
            raw_host = raw_host.split(":", 1)[0]
        host, _ = _decode(raw_host, ends[start:start + len(raw_host)], _PERCENT_RUN, _percent)
        host = host.translate(_DOTS).rstrip(".")
        # Sentence punctuation after a bare URL is not part of its hostname.
        host = host.rstrip(",;:!?)]}")
        try:
            normalized = host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            return None
        match = self.pattern.search(normalized)
        if not match:
            return None
        return span

    def replacements(self, text, *, url_value=False):
        spans = set()
        targets = []

        def add(target, start, *, explicit=False):
            if explicit:
                targets.append((start, start + len(target)))
            span = self.replacement_span(target)
            if span is not None:
                spans.add((start + span[0], start + span[1]))

        if url_value:
            # A structured URL field has an exact boundary, including punctuation.
            add(text, 0, explicit=True)
        for pattern in (_INLINE, _REFERENCE):
            for match in pattern.finditer(text):
                add(match["target"], match.start("target"), explicit=True)
        for tag in re.finditer(r"<[^>]+>", text):
            for match in _ATTRIBUTE.finditer(tag[0]):
                group = next(name for name, value in match.groupdict().items() if value is not None)
                add(match[group], tag.start() + match.start(group), explicit=True)
            # Markdown autolinks can contain a schemeless hostname.
            if self.replacement_span(tag[0]) is not None:
                add(tag[0], tag.start(), explicit=True)
        # Whole URLs consume paths and queries before hostname matching. Avoid
        # reinterpreting a URL inside an already-parsed Markdown/HTML destination.
        for match in _URL.finditer(text):
            if any(start <= match.start() < end for start, end in targets):
                continue
            target = match[0]
            # Keep sentence punctuation and unmatched enclosing delimiters.
            # Balanced parentheses in paths, e.g. /a_(b), belong to the URL.
            while target:
                if target[-1] in ".,;:!?":
                    target = target[:-1]
                elif target[-1] in ")}" and target.count(target[-1]) > target.count({")": "(", "}": "{"}[target[-1]]):
                    target = target[:-1]
                else:
                    break
            add(target, match.start())
        merged = []
        for start, end in sorted(spans):
            if merged and start < merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    def text(self, text, *, url_value=False):
        result = text
        for start, end in reversed(self.replacements(text, url_value=url_value)):
            result = result[:start] + REPLACEMENT + result[end:]
        return result

    @staticmethod
    def remap_index(index, spans, *, is_end=False):
        delta = 0
        for start, end in spans:
            if index < start:
                break
            if start <= index < end:
                # Keep a boundary before the placeholder at its original start. An
                # annotation overlapping hidden text covers the replacement.
                return start + delta + (len(REPLACEMENT) if is_end and index > start else 0)
            delta += len(REPLACEMENT) - (end - start)
        return index + delta

    def response(self, value):
        """Redact all string values, including citations and search-source URLs."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.response(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: self.text(item, url_value=True) if key in ("url", "uri", "href", "src") and isinstance(item, str)
                  else self.response(item) for key, item in value.items()}
        if value.get("type") == "output_text" and isinstance(value.get("text"), str):
            spans = self.replacements(value["text"])
            for annotation in result.get("annotations", []):
                if not isinstance(annotation, dict):
                    continue
                for key in ("start_index", "end_index"):
                    index = annotation.get(key)
                    if type(index) is int and 0 <= index <= len(value["text"]):
                        annotation[key] = self.remap_index(index, spans, is_end=key == "end_index")
        return result
