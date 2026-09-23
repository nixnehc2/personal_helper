"""Deterministic MIME parsing; never fetch remote HTML or extract attachments."""
from dataclasses import asdict, dataclass
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path

MAX_EMAIL_BYTES = 25 * 1024 * 1024


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.hidden += 1
        if tag in ("br", "p", "div", "li", "tr") and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


@dataclass
class ParsedEmail:
    message_id: str
    from_: str
    to: str
    cc: str
    subject: str
    date: str
    text_body: str
    html_body: str
    attachments: list
    in_reply_to: str
    references: str
    warnings: list

    def model_data(self):
        data = asdict(self)
        # HTML is retained by the parser, but only decoded readable text reaches the LLM.
        data.pop("html_body")
        data["body_truncated"] = len(self.text_body) > 30000
        data["text_body"] = self.text_body[:30000]
        return data


def read_raw_email(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_EMAIL_BYTES + 1)
    if len(raw) > MAX_EMAIL_BYTES:
        raise ValueError("email exceeds 25 MiB limit")
    return raw


def parse_email(path):
    return parse_bytes(read_raw_email(path))


def parse_bytes(raw):
    message = BytesParser(policy=policy.default).parsebytes(raw)
    plain, html, attachments, warnings = [], [], [], []

    def walk(part):
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        if disposition == "attachment" or filename or part.get_content_type() == "message/rfc822":
            payload = part.get_payload(decode=True)
            attachments.append(dict(filename=filename or "(unnamed)", content_type=part.get_content_type(),
                                    size=len(payload) if payload is not None else None,
                                    disposition=disposition, content_id=str(part.get("Content-ID", ""))))
            return
        if part.is_multipart():
            for child in part.iter_parts():
                walk(child)
            return
        payload = part.get_payload(decode=True) or b""
        if part.get_content_type() not in ("text/plain", "text/html"):
            attachments.append(dict(filename="(inline)", content_type=part.get_content_type(), size=len(payload)))
            return
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset)
        except (LookupError, UnicodeError):
            warnings.append("charset decode fallback: " + charset)
            text = payload.decode("utf-8", errors="replace")
        (plain if part.get_content_type() == "text/plain" else html).append(text)

    walk(message)
    text, markup = "\n".join(plain), "\n".join(html)
    if not text.strip() and markup:
        renderer = PlainHTML()
        renderer.feed(markup)
        text = "\n".join(line.strip() for line in "".join(renderer.parts).splitlines() if line.strip())
    warnings.extend(type(d).__name__ for d in message.defects)
    return ParsedEmail(*(str(message.get(key, "")) for key in ("Message-ID", "From", "To", "Cc", "Subject", "Date")),
                       text, markup, attachments, str(message.get("In-Reply-To", "")),
                       str(message.get("References", "")), warnings)
