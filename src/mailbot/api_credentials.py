"""Local shape checks for opaque Gemini credentials; never validate via a request."""


def valid_gemini_key(value: str) -> bool:
    # Auth keys differ from legacy keys. Do not assume a prefix, punctuation
    # scheme or legacy length. Reject unsafe HTTP header characters and keep
    # a finite size bound. Only the provider can establish authenticity.
    return (isinstance(value, str) and 20 <= len(value) <= 4096
            and all(33 <= ord(character) <= 126 for character in value))
