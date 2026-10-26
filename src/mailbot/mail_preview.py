"""Offline MIME extraction; this module never contacts Gmail or an LLM."""
from __future__ import annotations

import argparse
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
import json
from pathlib import Path
import re

MAX_MESSAGE_BYTES = 2 * 1024 * 1024


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('p', 'br', 'div', 'li'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def preview(raw: bytes, limit: int = 600) -> dict:
    """Return selected local text, explicitly not an AI-generated summary."""
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ValueError('message exceeds 2 MiB')
    if not 1 <= limit <= 4000:
        raise ValueError('limit must be between 1 and 4000')
    message = BytesParser(policy=policy.default).parsebytes(raw)
    body = message.get_body(preferencelist=('plain', 'html'))
    text = ''
    if body is not None and body.get_content_disposition() != 'attachment':
        content = body.get_content()
        if isinstance(content, str):
            text = content
            if body.get_content_type() == 'text/html':
                parser = _Text()
                parser.feed(text)
                text = ' '.join(parser.parts)
    # Remove control characters and collapse whitespace before display.
    clean = lambda s: re.sub(r'\s+', ' ', ''.join(c for c in s if c.isprintable() or c.isspace())).strip()
    text = clean(text)
    return {
        'mode': 'OFFLINE_EXTRACTIVE_PREVIEW',
        'llm': 'NOT_CONFIGURED',
        'subject': clean(str(message.get('subject', '')))[:300],
        'sender': clean(str(message.get('from', '')))[:300],
        'preview': text[:limit],
        'truncated': len(text) > limit,
        'attachments_processed': False,
    }


def main():
    parser = argparse.ArgumentParser(description='Local .eml preview; no network or AI calls. Output contains selected email text.')
    parser.add_argument('eml', type=Path)
    parser.add_argument('--limit', type=int, default=600)
    args = parser.parse_args()
    try:
        with args.eml.open('rb') as stream:
            raw = stream.read(MAX_MESSAGE_BYTES + 1)
        print(json.dumps(preview(raw, args.limit), ensure_ascii=False, indent=2))
    except (OSError, ValueError, LookupError):
        parser.exit(2, 'Unable to preview message: invalid input or unreadable file.\n')


if __name__ == '__main__':
    main()
