import re

# A media sha256 is exactly 64 hexadecimal characters. relay-manager's
# moderate-media endpoint rejects anything else, so callers (the moderation-result
# check and the age-restrict sink) validate against this before sending a hash.
HEX64_RE = re.compile(r'^[0-9a-f]{64}$', re.IGNORECASE)
