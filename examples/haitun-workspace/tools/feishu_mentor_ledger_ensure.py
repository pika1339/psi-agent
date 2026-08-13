"""Feishu/Lark mentor ledger tool — idempotently provision one mentor's TODO base.

Backs the "per-mentor 独立 Bitable 台账" design: each mentor gets their own Bitable
base (copied from a template, structure-only) rather than a shared table with
row-level visibility — Feishu's row-level isolation needs advanced permission, which
cannot be turned on once a base lives inside a wiki or is embedded in a doc (error
1254301). Isolation instead comes from Feishu's plain file permissions: a base only
that mentor (and the boss, read-only) can open.

Requires ``PSI_FEISHU_APP_ID`` / ``PSI_FEISHU_APP_SECRET`` + the ``bitable:app`` and
``drive:drive`` scopes, and needs a real person's Feishu identity for the copy step
(see the docstring below — Feishu's ``/copy`` endpoint rejects the bot's tenant token).
"""

from __future__ import annotations

# ruff: noqa: E402
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import _feishu_impl as _f


async def feishu_mentor_ledger_ensure(
    mentor_open_id: str,
    mentor_name: str,
    folder_token: str,
    template_app_token: str,
    boss_open_id: str = "",
    user_key: str = "",
    identity: str = "",
) -> str:
    """Idempotently ensure one mentor's TODO ledger base exists, granted to mentor + boss.

    Looks for a bitable already named "TODO 台账-<mentor_name>" directly in
    ``folder_token``; if found, returns its ``app_token``/``table_id`` without copying
    again (running this twice for the same mentor never produces a second base).
    Otherwise copies ``template_app_token`` (structure only — same columns, no rows)
    into that folder under that name.

    The copy step needs a real person's Feishu identity: Feishu's own
    ``/open-apis/bitable/v1/apps/:app_token/copy`` endpoint rejects the bot's tenant
    token outright (documented in the ``feishu-bitable`` skill), so this follows the
    same ownership-choice convention every other write tool here uses (see
    ``feishu_sheet_write``) — pass ``user_key`` (whose identity performs the copy) and
    optionally ``identity`` ("user"/"bot"); omit ``identity`` on first use and this
    returns ``need_identity_choice`` so you can ask, then it is remembered per
    ``user_key``.

    Grants ``mentor_open_id`` edit access and, if ``boss_open_id`` is given, view
    access — both through Feishu's documented file-permission grant. It does **not**
    grant the bot access to the new base: Feishu's permission member-type enum
    (openid/userid/unionid/openchat/opendepartmentid/email/groupid/wikispaceid) has no
    entry for "the app itself", so there is no supported way to add the bot as a
    collaborator through this endpoint. The result always carries
    ``bot_access: "not_granted"`` — if the bot's own later reads/writes against this
    specific base need to work without a live user's token, add the app as a
    collaborator once per base through the Feishu client (更多 → 协作者管理).

    Args:
        mentor_open_id: The mentor's open_id — the ledger is named and granted for them.
        mentor_name: The mentor's display name (used in the base title and to detect an
            already-provisioned ledger — must match exactly across calls).
        folder_token: The shared drive folder token all mentor ledgers live under.
        template_app_token: The app_token of the pre-built template base (columns
            already defined, no data) to copy from.
        boss_open_id: Optional — also grant this person read-only access.
        user_key: The sender's open_id whose identity performs the copy and grants.
        identity: "user" or "bot" — who owns the base if it has to be created. Omit to
            use this person's remembered choice.

    Returns:
        JSON with ok, app_token, table_id, created (bool — False when an existing
        ledger was found instead of copied), base_name, granted ({mentor, boss}),
        bot_access — or ok=false with a message (including ``need_identity_choice``/
        ``need_auth`` shapes) on failure.
    """
    return _f.dumps_result(
        await _f.mentor_ledger_ensure_impl(
            mentor_open_id, mentor_name, folder_token, template_app_token, boss_open_id, user_key, identity
        )
    )
