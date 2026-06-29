import logging
import random
import re

logger = logging.getLogger("linkedin_bot")

_SEPARATOR = re.compile(r"^\s*-{3,}\s*$", re.MULTILINE)
_DEFAULT_MESSAGE = "Hello {name}! I'd like to connect with you."


class MessageTemplates:
    """Load and personalize message templates from a text file.

    File format: one or more variations separated by a line of three or more
    dashes ("---"). Each variation may contain ``{name}`` which is replaced
    by the contact's first name at send time.

    Args:
        file_path: Path to the template file.
        max_length: If set, variations longer than this (in UTF-16 code units)
            have their ``{name}`` dropped; if still too long, they are
            truncated. Pass ``None`` (default) for no length enforcement
            (e.g. DMs, which have no LinkedIn character cap).
        default: Fallback text used when the file is missing or empty.
    """

    def __init__(self, file_path, max_length=None,
                 default=_DEFAULT_MESSAGE):
        self._max_length = max_length
        self._default = default
        self._templates = self._load(file_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def personalize(self, name=None):
        """Return a random variation with ``{name}`` filled in.

        If substituting ``name`` would exceed ``max_length``, the name is
        omitted.  If the template is still too long, it is hard-truncated.
        Returns an empty string when no templates are loaded (no-message mode).
        """
        if not self._templates:
            return ""

        template = random.choice(self._templates)

        if name:
            personalized = template.replace("{name}", name)
            if self._max_length is None or self._char_len(personalized) <= self._max_length:
                return personalized
            logger.warning(
                f"Message would be {self._char_len(personalized)} chars with "
                f"'{name}' (limit {self._max_length}). Omitting name for this send.")

        return self._drop_name(template)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load(self, file_path):
        if not file_path:
            return [self._default]
        try:
            import os
            if not os.path.exists(file_path):
                logger.warning(f"Message file '{file_path}' not found. Using default.")
                return [self._default]

            with open(file_path, encoding="utf-8") as f:
                raw = f.read()

            variations = [v.strip() for v in _SEPARATOR.split(raw)]
            variations = [v for v in variations if v]

            if not variations:
                logger.warning("Message file is empty. Using default.")
                return [self._default]

            if self._max_length is not None:
                cleaned = []
                for v in variations:
                    if self._char_len(v) > self._max_length:
                        v = v[: self._max_length]
                        logger.warning(
                            "A message variation exceeded "
                            f"{self._max_length} chars and was truncated.")
                    cleaned.append(v)
                variations = cleaned

            logger.info(
                f"Loaded {len(variations)} message variation(s) from '{file_path}'.")
            return variations

        except Exception as e:
            logger.error(f"Error loading message file: {e}. Using default.")
            return [self._default]

    @staticmethod
    def _char_len(text):
        """Count chars the way LinkedIn does: UTF-16 code units (emoji = 2)."""
        return len(text.encode("utf-16-le")) // 2

    def _drop_name(self, template):
        """Remove ``{name}`` and an adjacent comma/space so greeting reads cleanly."""
        result = re.sub(r",?\s*\{name\}", "", template)
        if self._max_length and self._char_len(result) > self._max_length:
            result = result[: self._max_length]
        return result
