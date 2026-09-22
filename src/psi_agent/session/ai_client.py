"""Left-side protocol adapter.  ``AiClient.stream()`` does HTTP→SSE
parsing→``AiDelta``.  Self-contained — depends only on the socket resolver
and protocol types.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import AsyncGenerator

import aiohttp
from loguru import logger

from psi_agent._sockets import resolve_connector_and_endpoint
from psi_agent.protocol import (
    FINISH_REASON_ERROR,
    SSE_DONE,
    parse_sse_data,
)
from psi_agent.session.protocol import AiDelta, ModelUsage

#: How many leading characters of ``content`` the fingerprint is computed over.
CONTENT_FINGERPRINT_CHARS = 32


def content_fingerprint(content: str) -> str:
    """A fingerprint of ``content``'s opening that **carries none of its characters**.

    The first attempt at this was a literal prefix, and its own test caught it
    leaking: an API key sitting at the start of a chunk went straight into the log
    line.  A prefix cannot be made safe by shortening it either — 32 characters is
    still a recognisable key.  So the fingerprint is two derived things instead:

    * ``h=`` a short BLAKE2s digest of the prefix.  Answers "do these two turns open
      *identically*", which is the question behind a repeated self-dialogue opener,
      and answers it without being invertible.
    * ``shape=`` a run-length skeleton of character *classes* (``H`` han, ``a`` lower,
      ``A`` upper, ``9`` digit, ``.`` punctuation/symbol, ``_`` space).  Answers "is
      this Chinese prose or a token-like blob" — the distinction that tells model
      self-narration apart from a normal reply.  A class skeleton reveals a secret's
      *shape*, never its value, and shape is not the secret.

    Both are stable across runs (no salt, no per-process seed): a fingerprint that
    changed per process could not be compared between two log lines, which is the
    only thing it is for.
    """
    prefix = content[:CONTENT_FINGERPRINT_CHARS]
    digest = hashlib.blake2s(prefix.encode("utf-8", "replace"), digest_size=4).hexdigest()

    def cls(ch: str) -> str:
        if "一" <= ch <= "鿿":
            return "H"
        if ch.isspace():
            return "_"
        if ch.isdigit():
            return "9"
        if ch.isupper():
            return "A"
        if ch.islower():
            return "a"
        return "."

    runs: list[str] = []
    for ch in prefix:
        c = cls(ch)
        if runs and runs[-1][0] == c:
            # Run-length: ``H12`` rather than twelve ``H``s, so one line stays short
            # even at the 32-character bound.
            head, _, count = runs[-1].partition("*")
            runs[-1] = f"{head}*{int(count or 1) + 1}"
        else:
            runs.append(c)
    shape = "".join(r.replace("*", "") if r.endswith("*1") else r for r in runs)
    return f"h={digest} shape={shape}"


def summarize_chunk_structure(data: dict) -> str:
    """One line describing an SSE chunk's *shape*, for the INFO log.

    **Why this exists.** The thinking-leak investigation could not find its root
    cause because all three places that could record raw SSE were at DEBUG, and
    production runs at INFO (``_run.py`` pins batch mode to INFO, and production
    *is* batch mode — there is no path that turns on global DEBUG in production).
    So the diagnosis needs a probe that production actually emits.

    **Why structure and not the text.** Two independent reasons:

    1. The raw body carries user content and API keys.  Logging it verbatim opens a
       new leak surface, and log files outlive the incident they were enabled for.
    2. A regex over the text was the *wrong judgment* last time.  Whether the model
       is talking to itself is answerable from ``tool_calls`` structure and from
       which of ``content`` / ``reasoning`` is populated — not from matching phrases,
       which differ per model and per language.

    **``reasoning_content`` equalling ``reasoning`` byte-for-byte is not a bug.** The
    AI layer mirrors the field under both spellings for compatibility; flagging it as
    an anomaly sent one investigation down a wrong path already.  It is reported here
    as a plain ``mirrored`` fact, and only a *differing* pair is called out.

    Returns the summary body, or ``""`` for a chunk with nothing to describe (the
    role-only opener, or a heartbeat).  Returning empty rather than
    ``reasoning=absent content=absent tool_calls=absent`` matters at the call site:
    that line is true of the first chunk of *every* healthy stream, so emitting it
    would spend the one-line-per-response budget on the least informative chunk and
    hide the shape the probe exists to record.
    """
    choices = data.get("choices")
    choice: dict = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    raw_delta = choice.get("delta")
    delta: dict = raw_delta if isinstance(raw_delta, dict) else {}

    reasoning = delta.get("reasoning")
    reasoning_content = delta.get("reasoning_content")
    content = delta.get("content")
    tool_calls_present = delta.get("tool_calls") is not None
    # ``content == ""`` is not substance: the opener carries an empty string.  An
    # explicit ``finish_reason`` is, since a bare finish chunk ends turns that
    # produced nothing and "ended empty" is itself the finding.
    if not (reasoning or reasoning_content or content or tool_calls_present or choice.get("finish_reason")):
        return ""

    parts: list[str] = []

    # Presence, not value.  "Did a reasoning field arrive at all" is the question the
    # leak investigation needed answered, and it is answerable without the text.
    if reasoning is None and reasoning_content is None:
        parts.append("reasoning=absent")
    else:
        spellings = (("reasoning", reasoning), ("reasoning_content", reasoning_content))
        which = "+".join(name for name, value in spellings if value is not None)
        parts.append(f"reasoning={which}")
        if reasoning is not None and reasoning_content is not None:
            # Mirrored is the normal case — see the docstring.  Only a mismatch is
            # worth a reader's attention, so the two cases get different words.
            parts.append("mirrored" if reasoning == reasoning_content else "reasoning_pair=DIFFER")

    parts.append(f"content={'absent' if content is None else f'{len(str(content))}c'}")

    tool_calls = delta.get("tool_calls")
    if tool_calls is None:
        parts.append("tool_calls=absent")
    elif isinstance(tool_calls, list):
        # The structural judgment: how many, at which indices, and whether each
        # carries a function name or is a continuation fragment.  This is what
        # distinguishes "the model is calling a tool" from "the model is writing
        # about calling a tool" — the latter has no tool_calls at all.
        shapes: list[str] = []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                shapes.append(f"?{type(tc).__name__}")
                continue
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            name = fn.get("name")
            args = fn.get("arguments")
            shapes.append(
                f"[{tc.get('index', '?')}]"
                f"{'name' if name else 'nameless'}"
                f"/{'args' + str(len(str(args))) + 'c' if args else 'noargs'}"
            )
        parts.append(f"tool_calls={len(tool_calls)}({','.join(shapes) or '-'})")
    else:
        parts.append(f"tool_calls=MALFORMED({type(tool_calls).__name__})")

    if content:
        # A *derived* fingerprint, never the characters themselves — see
        # ``content_fingerprint``.  The literal-prefix version of this line leaked an
        # API key in its own test.
        parts.append(content_fingerprint(str(content)))

    if (finish := choice.get("finish_reason")) is not None:
        parts.append(f"finish={finish}")

    return " ".join(parts)


class AiClient:
    """Protocol adapter for the AI backend — handles HTTP/SSE and yields AiDelta."""

    def __init__(self, ai_socket: str) -> None:
        self.ai_socket = ai_socket

    def _build_connector_and_endpoint(self) -> tuple[aiohttp.BaseConnector, str]:
        return resolve_connector_and_endpoint(self.ai_socket)

    @staticmethod
    def _as_int(value: object) -> int:
        """Coerce an untrusted SSE field to int; 0 when absent or malformed.

        ``bool`` is rejected explicitly: it is a subclass of ``int``, so a JSON
        ``true`` would otherwise silently become ``1`` token.
        """
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                return 0
        return 0

    @classmethod
    def _usage_prompt_tokens(cls, data: dict) -> int:
        """Prompt tokens from a chunk's ``usage``, or 0 when absent.

        The AI layer forces ``stream_options.include_usage`` and forwards every
        upstream chunk verbatim, so this number is already on the wire — it was
        simply never parsed.  Reading it here rather than adding a second signal
        keeps one fact on one path.
        """
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return 0
        return cls._as_int(usage.get("prompt_tokens"))

    @staticmethod
    def _opt_int(value: object) -> int | None:
        """Coerce an untrusted SSE field to int, or ``None`` when **not measured**.

        Deliberately *not* ``_as_int``: that one returns 0 for a missing field, and
        its caller (the prompt budget) is right to want that — a low token estimate
        means an under-packed request, which is safe.  In a cost total the same 0
        asserts "this call was free", so the absence has to stay distinguishable
        all the way to the jsonl line.

        ``bool`` is rejected for the same reason ``_as_int`` rejects it — it is an
        ``int`` subclass, so a JSON ``true`` would otherwise become 1 token — but
        the result here is ``None``, not 0: a boolean in a token field means the
        value could not be read, which is a measurement gap rather than a zero.
        """
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                return None
        return None

    @classmethod
    def _usage(cls, data: dict) -> ModelUsage | None:
        """Full cost inputs from a chunk's ``usage``, or ``None`` when absent.

        Shape confirmed against the installed ``any_llm`` ``CompletionUsage``, which
        the AI layer forwards verbatim via ``chunk.model_dump_json()``:
        ``prompt_tokens`` / ``completion_tokens`` / ``total_tokens`` plus the two
        OpenAI detail sub-dicts, ``prompt_tokens_details.cached_tokens`` and
        ``completion_tokens_details.reasoning_tokens``.

        **On ``reasoning_tokens`` specifically:** the field exists in the schema and
        is read here, but this deployment's usual path is unlikely to populate it —
        any-llm's DeepSeek provider injects ``thinking={"type": "disabled"}`` when no
        ``reasoning_effort`` is passed, and while ``ai/server.py`` now defaults that
        to ``"medium"`` for DeepSeek, whether the upstream then *reports*
        ``reasoning_tokens`` back was **not** verified against a live provider (no
        such call was made from here).  Hence ``None`` rather than 0 when it is
        missing: an unconfirmed field must read as "not measured", never as "the
        model did no thinking".

        A detail sub-dict that is absent or not a dict leaves its fields ``None``
        while ``reported`` stays ``True`` — usage did arrive, those parts of it did
        not.
        """
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return None
        prompt_details = usage.get("prompt_tokens_details")
        completion_details = usage.get("completion_tokens_details")
        model = data.get("model")
        return ModelUsage(
            reported=True,
            model=model if isinstance(model, str) and model else None,
            prompt_tokens=cls._opt_int(usage.get("prompt_tokens")),
            completion_tokens=cls._opt_int(usage.get("completion_tokens")),
            total_tokens=cls._opt_int(usage.get("total_tokens")),
            cached_tokens=(
                cls._opt_int(prompt_details.get("cached_tokens")) if isinstance(prompt_details, dict) else None
            ),
            reasoning_tokens=(
                cls._opt_int(completion_details.get("reasoning_tokens"))
                if isinstance(completion_details, dict)
                else None
            ),
        )

    async def stream(self, request_body: dict) -> AsyncGenerator[AiDelta]:
        connector, endpoint = self._build_connector_and_endpoint()
        # Serialize once ourselves instead of passing ``json=``: aiohttp would
        # run the same ``json.dumps`` internally, so doing it here buys the exact
        # request byte count for free.  Bytes are half the latency model —
        # ``delay ≈ bytes / bandwidth`` — and without them a slow turn cannot be
        # attributed (bigger request vs. worse bandwidth).  Note ``prompt_budget``
        # counts *characters*, which at ~3.47 bytes/char for Chinese is not a
        # usable substitute.
        payload = json.dumps(request_body).encode()
        t0 = time.monotonic()
        ttft_logged = False
        structure_logged = False
        async with (
            aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=None)) as session,
            session.post(endpoint, data=payload, headers={"Content-Type": "application/json"}) as resp,
        ):
            # First hop only (this process → ``psi_agent.ai.server`` over the
            # local socket): steady 50-70ms, independent of context size.  It is
            # *not* the first token — reading this line as TTFB is what produced
            # the since-retracted "upstream TTFB 0.18s" claim.
            t_headers = time.monotonic() - t0
            logger.info(f"AI response status: {resp.status} (第一跳响应头 {t_headers:.3f}s, 非首字)")
            if resp.status != 200:
                error_text = await resp.text()
                logger.error(f"AI error from {self.ai_socket!r}: {error_text[:1000]!r}")
                yield AiDelta(finish_reason=FINISH_REASON_ERROR, content=f"[AI Error: {resp.status}]")
                return

            logger.debug("Starting to consume SSE stream")
            async for raw_line in resp.content:
                line = raw_line.decode().strip()
                data_str = parse_sse_data(line)
                # Empty payloads are heartbeats on some OpenAI-compatible
                # servers; skip them silently rather than letting them reach
                # ``json.loads`` and log a warning per beat.
                if not data_str or data_str == SSE_DONE:
                    continue

                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    logger.warning(f"Failed to parse SSE data: {data_str[:1000]!r}")
                    continue

                # W#9 structural summary, beside the one place that *could* record
                # raw SSE — and at INFO, because production runs at INFO and a DEBUG
                # probe here emits nothing at all.  Never the raw body: it carries
                # user content and keys (see ``summarize_chunk_structure``).
                #
                # Once per response, on the first chunk that has any substance.  Not
                # per chunk: a turn is hundreds of deltas, and a per-chunk INFO line
                # would bury the rest of the log — the volume itself becomes a new
                # blind spot.  Not per turn either, since ``stream()`` is called once
                # per model round and each round's shape is its own question.
                if not structure_logged:
                    summary = summarize_chunk_structure(data)
                    if summary:
                        structure_logged = True
                        logger.info(f"AI chunk structure: {summary}")

                usage_prompt_tokens = self._usage_prompt_tokens(data)
                usage = self._usage(data)

                choices_data = data.get("choices", [])
                if not isinstance(choices_data, list):
                    logger.warning(f"Expected choices as list, got {type(choices_data).__name__}")
                    continue
                if not choices_data and (usage_prompt_tokens or usage is not None):
                    # OpenAI sends the final usage chunk with an *empty* choices
                    # array, so the `continue` below would drop the one number
                    # the budget calibrates against.  Surface it as a delta that
                    # carries nothing else.
                    #
                    # Gated on ``usage is not None`` as well as on the number: a
                    # usage object reporting a genuine 0 (or carrying only the
                    # detail sub-dicts) is falsy, and dropping it here would put
                    # cost accounting back to guessing on exactly the turns the
                    # upstream did report.  A bare ``{"choices": []}`` heartbeat
                    # still falls through to the skip below.
                    yield AiDelta(usage_prompt_tokens=usage_prompt_tokens, usage=usage)
                    continue
                if len(choices_data) > 1:
                    logger.warning(f"Expected 1 choice, got {len(choices_data)}, yielding error")
                    yield AiDelta(
                        finish_reason=FINISH_REASON_ERROR,
                        content=f"[AI Error: expected 1 choice, got {len(choices_data)}]",
                    )
                    return
                if not choices_data:
                    continue

                c = choices_data[0]
                if not isinstance(c, dict):
                    logger.warning(f"Expected choice as dict, got {type(c).__name__}")
                    continue
                delta_data = c.get("delta")
                if not isinstance(delta_data, dict):
                    delta_data = {}
                # Time to first token, measured once per turn.  ``reasoning``
                # counts as a first token because the card renders thinking live —
                # what the user sees first is usually reasoning, so keying on
                # ``content`` alone would systematically overstate the wait.
                #
                # Scope: excludes queue wait (``t0`` is already past the queue;
                # enqueue→dequeue is a separate probe, not done here).  ``req_bytes``
                # is the body sent to ``ai.server``, which then injects
                # ``stream_options.include_usage`` before forwarding — so it runs a
                # few dozen bytes under the true on-wire size and should not be
                # read as an exact line count.
                #
                # INFO, deliberately: production runs at INFO, and a DEBUG probe
                # here emits nothing at all.
                #
                # The same measurement rides out on the delta (``ttft_s`` /
                # ``req_bytes``) so the turn's metrics row can carry it: a log line
                # is not readable by the cost report, and re-deriving TTFT at the
                # agent layer is what would reintroduce the header-moment mistake.
                ttft_s: float | None = None
                req_bytes: int | None = None
                if not ttft_logged:
                    reasoning_first = delta_data.get("reasoning")
                    if reasoning_first or delta_data.get("content"):
                        ttft_logged = True
                        ttft_s = time.monotonic() - t0
                        req_bytes = len(payload)
                        logger.info(
                            f"AI first token: ttft={ttft_s:.3f}s "
                            f"req_bytes={req_bytes} "
                            f"kind={'reasoning' if reasoning_first else 'content'}"
                        )
                compaction_signal = data.get("psi_compaction", {})
                compaction_needed = isinstance(compaction_signal, dict) and compaction_signal.get("needed", False)
                yield AiDelta(
                    content=delta_data.get("content"),
                    reasoning=delta_data.get("reasoning"),
                    kind=delta_data.get("kind") if isinstance(delta_data.get("kind"), str) else None,
                    tool_calls=delta_data.get("tool_calls"),
                    finish_reason=c.get("finish_reason"),
                    ttft_s=ttft_s,
                    req_bytes=req_bytes,
                    compaction_needed=compaction_needed,
                    prompt_tokens=self._as_int(compaction_signal.get("prompt_tokens"))
                    if isinstance(compaction_signal, dict)
                    else 0,
                    compaction_threshold=self._as_int(compaction_signal.get("threshold"))
                    if isinstance(compaction_signal, dict)
                    else 0,
                    usage_prompt_tokens=usage_prompt_tokens,
                    usage=usage,
                )
            logger.debug("SSE stream consumed successfully")
