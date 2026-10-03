"""Turn one page of manual text into a plain-language version with Claude."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("CLAUDE_EFFORT", "medium")
GLOSSARY_PATH = Path(os.environ.get("GLOSSARY_PATH", "/data/glossary.txt"))

# USD per million tokens, used only for the cost estimate shown on the page.
PRICE_IN = float(os.environ.get("PRICE_INPUT_PER_MTOK", "4"))
PRICE_OUT = float(os.environ.get("PRICE_OUTPUT_PER_MTOK", "20"))

LANGUAGES = {"zh": "Simplified Chinese", "en": "English"}

SYSTEM_PROMPT = """\
You rewrite pages of airline manuals (for example Emirates flight operations, cabin crew and \
engineering manuals) into a plain-language version that a new crew member understands on \
first read and remembers easily.

You receive the text blocks of one PDF page as JSON. Each block sits in a fixed box on the \
page, so for every block return one rewritten text in the requested target language. The \
source may be Chinese, English or both; always write in the target language.

Rules:
- Keep the meaning exact. Never drop, soften or add a limit, number, unit, step, condition, \
warning or exception. Keep the order of steps.
- Keep numbers, units, part numbers, reference codes (for example SOP 2.3.1), aircraft types \
and switch or panel labels exactly as written. Keep common abbreviations (N1, ATC, MEL); \
explain one in a few words only when that makes the sentence clear.
- Use short, direct sentences and everyday words. Prefer active voice and "do X" over \
"the crew shall ensure that X".
- Keep WARNING / CAUTION / NOTE labels, translated into the target language (for Chinese: \
警告 / 注意 / 说明).
- Each block must fit its original box: aim for about the same length as the source, or \
shorter. Headings stay headings; table cells stay short.
- If a block is a name, label or fragment that needs no rewrite, return it translated or \
unchanged.
- Return every block id exactly once.
"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "blocks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "text": {"type": "string"}},
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["blocks"],
    "additionalProperties": False,
}


class RewriteError(Exception):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens

    @property
    def cost_usd(self) -> float:
        billed_in = self.input_tokens + self.cache_write_tokens * 1.25 + self.cache_read_tokens * 0.1
        return (billed_in * PRICE_IN + self.output_tokens * PRICE_OUT) / 1_000_000


@dataclass
class RewriteResult:
    texts: dict[int, str]
    usage: Usage = field(default_factory=Usage)


def _system_blocks() -> list[dict]:
    text = SYSTEM_PROMPT
    if GLOSSARY_PATH.is_file():
        glossary = GLOSSARY_PATH.read_text(encoding="utf-8").strip()
        if glossary:
            text += "\nAlways use these term translations:\n" + glossary + "\n"
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


class ClaudeRewriter:
    def __init__(self) -> None:
        self.client = anthropic.Anthropic(max_retries=6)
        self.system = _system_blocks()

    def rewrite(self, blocks: list[dict], target_lang: str, page_no: int) -> RewriteResult:
        """blocks: [{"id": int, "text": str}]. Returns rewritten text keyed by id."""
        if not blocks:
            return RewriteResult({})
        payload = {
            "target_language": LANGUAGES[target_lang],
            "page": page_no,
            "blocks": blocks,
        }
        response = self.client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=self.system,
            output_config={
                "effort": EFFORT,
                "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
            },
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        )
        u = response.usage
        usage = Usage(
            u.input_tokens,
            u.output_tokens,
            u.cache_read_input_tokens or 0,
            u.cache_creation_input_tokens or 0,
        )

        if response.stop_reason == "refusal":
            raise RewriteError(f"Claude declined page {page_no}")
        if response.stop_reason == "max_tokens":
            if len(blocks) == 1:
                raise RewriteError(f"Page {page_no}: one block is too long to rewrite")
            # Too much text for one answer: do the page in two halves.
            mid = len(blocks) // 2
            first = self.rewrite(blocks[:mid], target_lang, page_no)
            second = self.rewrite(blocks[mid:], target_lang, page_no)
            first.usage.add(usage)
            first.usage.add(second.usage)
            return RewriteResult({**first.texts, **second.texts}, first.usage)

        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise RewriteError(f"Page {page_no}: unreadable answer ({e})") from e
        wanted = {b["id"] for b in blocks}
        texts = {b["id"]: b["text"] for b in data.get("blocks", []) if b.get("id") in wanted}
        return RewriteResult(texts, usage)


class MockRewriter:
    """Offline stand-in used for layout testing: tags each block instead of calling Claude."""

    def rewrite(self, blocks: list[dict], target_lang: str, page_no: int) -> RewriteResult:
        tag = "【测试】" if target_lang == "zh" else "[TEST] "
        return RewriteResult({b["id"]: tag + b["text"] for b in blocks})


def make_rewriter():
    if os.environ.get("REWRITER", "claude") == "mock":
        return MockRewriter()
    return ClaudeRewriter()
