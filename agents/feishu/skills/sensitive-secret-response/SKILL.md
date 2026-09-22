---
name: sensitive-secret-response
description: "When a user pastes an API key/token or a file/tool result contains one (敏感串, sk-…, JWT, api_key=…): scrub local logs/history via secret_scrub, then FORCE a risk notice + key-rotation advice. Skip the notice only if the user explicitly insists there is no security risk."
category: knowledge-base
---

# Sensitive secret response（密钥 / 敏感串）

## When to use

Load and follow this skill **before ordinary answering** when any of the following is true in the current turn:

- The user pastes or types a likely secret (`sk-…`, JWT `eyJ…`, `Bearer …`, `api_key=` / `token=` / `password=` with a long value).
- A tool result (`read` / `bash` / file preview) contains such a string — including files named like `敏感串.txt`.
- The user asks you to check logs/history **for** a secret they just shared.

This is a **user-behavior** path (they put the secret into the chat or a local file). The product response is: scrub local copies + risk disclosure — not “pretend logs never saw it.”

## When not to use

- Authorized security work on systems the user owns, with **placeholder** secrets only (`<API_KEY>`, `sk-***`).
- Discussing secret-handling policy without any concrete secret value in view.
- The user already completed scrub+notice this turn and is continuing a different task.
- You still need a key for a tool but the user has **not** pasted one — use `skills/env-api-key-setup/SKILL.md` (write env file + self-fill) instead of asking them to paste into chat.

## User insists — skip notice (only)

If the **same** user message (or the immediate prior user message) clearly insists there is **no security risk** / they will **not** rotate, skip the forced risk paragraph and key-rotation advice.

Accept phrases (Chinese or English), examples:

- 「没有安全风险」「不必更换」「坚持不换」「我知道风险，继续」「无需告知」
- “no security risk”, “don’t rotate”, “I accept the risk”

Still **do scrub** if a concrete secret is present (local hygiene), unless they also forbid scrubbing. Never echo the plaintext secret back.

## Instructions (mandatory order)

1. **Extract** the secret values from the user message and/or tool result. Do **not** put them in your assistant reply.
2. Call **`secret_scrub`**:
   - Prefer `secrets_json='["…"]'` with the exact strings.
   - Optionally also pass `text=<the blob that contained them>` so patterns can catch variants.
   - Default scopes (history + logs + metrics) are fine; use `session_only=true` only when the user asks to limit to this chat.
3. If scrub returns `ok=false` / `replacements=0`, say you attempted scrub and that brief local copies may still exist — still give the risk notice (unless insist).
4. **Force** the risk notice below as the primary user-facing reply (or the lead of the reply). Do not answer the original ask first and bury the notice.
5. **Suggest key rotation** at the issuer (OpenAI / Anthropic / cloud console / Feishu app, etc.). Advise not to paste plaintext keys into chat again; for ongoing BYOK setup point them to `skills/env-api-key-setup/SKILL.md` (env file + self-fill).
6. Only after the notice: answer the user’s original question **without** repeating the secret. Refer by role (`API key`, `token`), never value.

## Forced risk notice (copy / adapt; keep meaning)

Use Chinese for Chinese users:

```text
【安全风险告知】对话或本地文件中出现了疑似密钥/凭据。这些内容可能已短暂写入本机会话历史或调试日志。
我已尝试从本机相关记录中洗除命中串（替换为 [REDACTED_SECRET]）。
请尽快在签发方轮换或作废该密钥，并避免再次把明文密钥粘贴进对话。
若你确认「没有安全风险、不必更换」，明确回复即可；否则请先完成更换。
```

## Boundaries

- Never write the secret into new files, code samples, or `[SEND:]` deliverables.
- Never claim “logs never contained secrets” after a paste/`read` — claim scrub + rotation instead.
- Do not refuse the whole turn; complete scrub + notice, then help with the task using placeholders.
