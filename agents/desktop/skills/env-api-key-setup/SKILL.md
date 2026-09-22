---
name: env-api-key-setup
description: "When a task needs the user's API key (BYOK / third-party service): write an env config file with placeholders first, then instruct the user to set KEY=value themselves. Never ask them to paste the key into chat. Only if they insist you write it in, do so — then follow sensitive-secret-response (scrub + risk notice)."
category: knowledge-base
---

# Env API key setup（需要用户 API key）

## When to use

Load and follow this skill **before asking for credentials** when any of the following is true:

- A tool / skill reports missing API config (`ok: false`, “API 未配置”, empty `*_API_KEY`, placeholder `YOUR_*_HERE`).
- You are about to call a BYOK service (image gen, TTS/STT, Serper, Trello, Gemini, X, Haibao, etc.) and the env is not set.
- The user asks how to configure an API key for this workspace / tool.

This is the **default credential path**. Chat is not a key vault.

## When not to use

- Gateway Hub / spa-v2 model pool (user connects models in the UI — do not invent a parallel `.env` for that).
- Feishu bot credentials already managed by deployment (`PSI_FEISHU_APP_*`) — do not ask the user for app_secret.
- The env file already has a real value and the tool failure is unrelated (quota, network, wrong host).

## Hard rules

1. **Never** open with 「请把 API key 发给我 / 粘贴到对话里」.
2. **First** write (or update) an env config file under the **user workspace** with the required variable names and **placeholders only** (e.g. `MINIMAX_API_KEY=YOUR_MINIMAX_API_KEY_HERE`). Prefer the file the domain skill already names (`.env.multimodal`, `.env`, etc.); otherwise create `.env` / `.env.<service>` and tell the user which file.
3. **Then** tell the user to put the real value into that file **themselves**, or set the same names as OS / shell environment variables (`KEY=value` form). Give the exact variable names; do not invent alternate names.
4. After they configure, re-run the tool. Do not stall asking for the secret in chat.
5. **Exception — user insists:** only if the user **explicitly** insists that you write the key into the config for them (e.g. 「你帮我写进配置」「直接填进去」「我坚持把 key 给你」), you may write the value into the env file **once**. Immediately after that write (same turn), you **MUST** follow `skills/sensitive-secret-response/SKILL.md`: `secret_scrub` + forced risk notice + key-rotation advice (unless they also insist there is no security risk). Do not echo the key in the reply.

Accept insist phrases (Chinese or English), examples:

- 「帮我写进配置」「你直接填」「坚持把 key 给你」「就粘贴给你写」
- “paste it for me”, “you write it into .env”, “I insist you fill it in”

Ambiguous 「帮我配一下」 alone is **not** insist — still use the placeholder + self-fill path and ask them to edit the file.

## Instructions (mandatory order)

1. Identify which env var(s) and which config file the domain skill / tool expects.
2. If the file is missing or only has placeholders: `write` / `edit` it with placeholders; never invent a live key.
3. Reply with: file path + variable names + short how-to (edit file or set env). Stop asking for the secret in chat.
4. If the user pastes a key without insist-to-write: do **not** silently save it — follow `sensitive-secret-response` (scrub + notice) and point them back to the env file path.
5. If they insist you write it: write the key into the agreed env file → call `secret_scrub` with that value → deliver the forced risk notice from `sensitive-secret-response` → continue the original task without repeating the key.

## Example reply shape (Chinese)

```text
这个能力需要本地配置 API key，我不会在对话里向你要明文密钥。

已在工作区写好配置文件：`<path>`（变量如 `FOO_API_KEY=YOUR_…_HERE`）。
请你本地把真实值填进该文件，或在系统/终端环境里设置同名变量（`FOO_API_KEY=…`），配好后告诉我再试一次即可。

若你坚持把 key 发给我、让我代写进配置，也可以——但写入后我会按安全流程洗除本机相关记录，并强制提示风险与建议轮换密钥。
```

## Boundaries

- Prefer merge/update of existing env files; do not clobber unrelated keys unless the user asks to replace the file.
- Never put secrets into `skills/`, `[SEND:]` deliverables, sample code, or commit suggestions.
- Domain skills (e.g. `image-generation`) that already say “user fills `.env.multimodal`” stay authoritative for **which** file/vars; this skill owns the **credential-collection** procedure.
