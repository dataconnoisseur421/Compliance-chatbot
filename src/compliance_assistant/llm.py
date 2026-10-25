"""The model layer. Everything else talks to ``ChatModel`` so the provider can be swapped."""

from __future__ import annotations

import os
from typing import Protocol


class ChatModel(Protocol):
    name: str

    def complete(self, system: str, user: str, max_tokens: int = 1500) -> str: ...


class LLMError(RuntimeError):
    pass


class AnthropicModel:
    """Hosted Claude. Needs ``pip install -e '.[llm]'`` and ANTHROPIC_API_KEY in the environment."""

    def __init__(self, model: str | None = None):
        self.name = model or os.environ.get("ANSWER_MODEL", "claude-sonnet-5-5")
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMError("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key, "
                           "or export it in your shell.")
        try:
            import anthropic
        except ImportError as exc:
            raise LLMError("Install the client: pip install -e '.[llm]'") from exc
        self._client = anthropic.Anthropic()

    def complete(self, system: str, user: str, max_tokens: int = 1500) -> str:  # pragma: no cover - network
        msg = self._client.messages.create(
            model=self.name, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


class FakeModel:
    """Returns a canned reply (or one built from the prompt) so the pipeline can be tested offline."""

    name = "fake"

    def __init__(self, reply: str | None = None):
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, max_tokens: int = 1500) -> str:
        self.calls.append((system, user))
        return self.reply if self.reply is not None else "Answer based on the sources. [1]"


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env reader so the key never has to be typed on the command line."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
