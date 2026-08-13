"""Feishu Mentor Ledger — idempotently provision one mentor's TODO-tracking Bitable base.

Split out of ``_feishu_impl.py`` by domain, following the same shape as the other
``_feishu/*.py`` modules. Reaches the shared client/token layer through ``_core``.

Backs the "per-mentor 独立 Bitable 台账" design: rather than one shared base with
row-level visibility (which needs Feishu's advanced permission, unavailable once a
base lives inside a wiki or is embedded in a doc — error 1254301), each mentor gets
their own base copied from a template, with isolation coming from Feishu's plain file
permissions instead. See the ``feishu-bitable`` skill (``copy`` is a **template**
operation, needs a user token) and ``feishu-permission`` skill (member grants).

Three actions have to happen exactly once no matter how many times this runs, and
failing partway must not leave a half-provisioned ledger invisible to everyone:
finding whether the base already exists, copying the template only if it doesn't,
and resolving the one table inside it. Doing any of this from a prompt would risk
double-copying (a second base named the same) or leaving out one of the two grants
this module *can* make (see the ``bot_access`` note in ``mentor_ledger_ensure_impl``).
"""

from __future__ import annotations

from typing import Any

import _feishu_impl as _core
from lark_channel.core.enum import AccessTokenType, HttpMethod
from lark_channel.core.model import BaseRequest

_LEDGER_NAME_PREFIX = "TODO 台账-"


def _ledger_base_name(mentor_name: str) -> str:
    return f"{_LEDGER_NAME_PREFIX}{mentor_name.strip()}"


def _build_list_folder_request(folder_token: str, page_token: str) -> BaseRequest:
    req = BaseRequest()
    req.http_method = HttpMethod.GET
    req.uri = "/open-apis/drive/v1/files"
    req.add_query("folder_token", folder_token)
    req.add_query("page_size", "100")
    if page_token:
        req.add_query("page_token", page_token)
    req.token_types = {AccessTokenType.TENANT, AccessTokenType.USER}
    return req


async def _find_existing_base(folder_token: str, base_name: str) -> tuple[str | None, dict[str, Any] | None]:
    """Look for a bitable file named ``base_name`` directly in ``folder_token``.

    Returns (app_token_or_None, error_or_None). Listing does **not** recurse — the
    ledger folder is expected to be flat (one file per mentor) — matching
    ``feishu-drive``'s own note that this endpoint lists one level only.
    """
    page_token = ""
    while True:
        res = await _core._invoke(_build_list_folder_request(folder_token, page_token))
        if not res["ok"]:
            return None, res
        data = res["data"] if isinstance(res["data"], dict) else {}
        for f in data.get("files", []) if isinstance(data.get("files"), list) else []:
            if isinstance(f, dict) and f.get("type") == "bitable" and f.get("name") == base_name:
                token = f.get("token", "")
                return (token or None), None
        page_token = data.get("page_token", "") or ""
        if not data.get("has_more") or not page_token:
            return None, None


def _build_copy_app_request(template_app_token: str, name: str, folder_token: str) -> BaseRequest:
    req = BaseRequest()
    req.http_method = HttpMethod.POST
    req.uri = "/open-apis/bitable/v1/apps/:app_token/copy"
    req.paths["app_token"] = template_app_token
    # Copy is documented as user-token-only (feishu-bitable skill) — Feishu rejects
    # the bot's tenant token on this endpoint, unlike most other bitable writes.
    req.token_types = {AccessTokenType.USER}
    req.body = {"name": name, "folder_token": folder_token, "without_content": True}
    return req


def _build_list_tables_request(app_token: str) -> BaseRequest:
    req = BaseRequest()
    req.http_method = HttpMethod.GET
    req.uri = "/open-apis/bitable/v1/apps/:app_token/tables"
    req.paths["app_token"] = app_token
    req.add_query("page_size", "20")
    req.token_types = {AccessTokenType.TENANT, AccessTokenType.USER}
    return req


async def _first_table_id(app_token: str, user_key: str) -> tuple[str | None, dict[str, Any] | None]:
    res = await _core._invoke(_build_list_tables_request(app_token), user_key=user_key)
    if not res["ok"]:
        return None, res
    data = res["data"] if isinstance(res["data"], dict) else {}
    items = data.get("items", []) if isinstance(data.get("items"), list) else []
    if not items:
        return None, _core._error(f"Base {app_token!r} has no tables — check the template base has one 待办 table.")
    first = items[0] if isinstance(items[0], dict) else {}
    table_id = first.get("table_id", "")
    if not table_id:
        return None, _core._error(f"Base {app_token!r}'s first table has no table_id in the response.")
    return table_id, None


def _build_grant_member_request(token: str, member_id: str, perm: str) -> BaseRequest:
    req = BaseRequest()
    req.http_method = HttpMethod.POST
    req.uri = "/open-apis/drive/v1/permissions/:token/members"
    req.paths["token"] = token
    req.add_query("type", "bitable")
    req.add_query("need_notification", "false")
    req.token_types = {AccessTokenType.TENANT, AccessTokenType.USER}
    req.body = {"member_type": "openid", "member_id": member_id, "perm": perm, "type": "user"}
    return req


async def _grant(token: str, member_id: str, perm: str, user_key: str, identity: str) -> dict[str, Any]:
    return await _core._invoke(
        _build_grant_member_request(token, member_id, perm), user_key=user_key, prefer="user", identity=identity
    )


async def mentor_ledger_ensure_impl(
    mentor_open_id: str,
    mentor_name: str,
    folder_token: str,
    template_app_token: str,
    boss_open_id: str = "",
    user_key: str = "",
    identity: str = "",
) -> dict[str, Any]:
    """Idempotently ensure one mentor's TODO ledger base exists, granted to mentor + boss.

    First lists ``folder_token`` for a bitable already named "TODO 台账-<mentor_name>";
    if found, returns its ``app_token``/``table_id`` without copying again. Otherwise
    copies ``template_app_token`` (structure only, ``without_content=True``) into that
    folder under that name — this step needs a real person's Feishu identity (Feishu's
    ``/copy`` endpoint rejects the bot's tenant token), so ``user_key``/``identity``
    follow the same ownership-choice convention as every other write tool here (see
    ``feishu_sheet_write``): omit and this returns ``need_identity_choice`` on first use,
    then remembers the choice.

    Grants ``mentor_open_id`` edit and, if given, ``boss_open_id`` view — both via the
    documented ``drive/v1/permissions`` member grant. It does **not** grant the bot
    access: Feishu's permissions member-type enum (openid/userid/unionid/openchat/
    opendepartmentid/email/groupid/wikispaceid) has no entry for "the app itself", so
    the bot cannot be added as a collaborator through this endpoint. The result's
    ``bot_access`` field is always ``"not_granted"`` — add the app as a collaborator
    once per base through the Feishu client (更多 → 协作者管理) if the bot's own
    later reads/writes against this base need it.

    Args:
        mentor_open_id: The mentor's open_id — the ledger is named and granted for them.
        mentor_name: The mentor's display name, used in the base's title
            ("TODO 台账-<mentor_name>") and to detect an already-provisioned ledger.
        folder_token: The shared drive folder all mentor ledgers live under.
        template_app_token: The app_token of the pre-built template base (columns
            already defined, no data) to copy from.
        boss_open_id: Optional — grant this person read-only access too.
        user_key: The sender's open_id whose identity performs the copy/grants.
        identity: ``"user"`` or ``"bot"`` — who owns the resulting base if it has to be
            created. Omit to use this person's remembered choice.

    Returns:
        JSON with ok, app_token, table_id, created (bool — False when an existing
        ledger was found), base_name, granted ({mentor, boss}), bot_access — or
        ok=false with a message (including ``need_identity_choice``/``need_auth``
        shapes) on failure.
    """
    if not mentor_open_id.strip():
        return _core._error("mentor_open_id is required.")
    if not mentor_name.strip():
        return _core._error("mentor_name is required.")
    if not folder_token.strip():
        return _core._error("folder_token is required (the shared drive folder for mentor ledgers).")
    if not template_app_token.strip():
        return _core._error("template_app_token is required (the pre-built template base to copy).")

    base_name = _ledger_base_name(mentor_name)
    app_token, err = await _find_existing_base(folder_token.strip(), base_name)
    if err is not None:
        return err

    created = False
    if app_token is None:
        copy_res = await _core._invoke(
            _build_copy_app_request(template_app_token.strip(), base_name, folder_token.strip()),
            user_key=user_key,
            prefer="user",
            identity=identity,
        )
        if not copy_res["ok"]:
            return copy_res
        data = copy_res["data"] if isinstance(copy_res["data"], dict) else {}
        app = data.get("app", {}) if isinstance(data.get("app"), dict) else {}
        app_token = app.get("app_token", "")
        if not app_token:
            return _core._error(f"Copy succeeded but the response carried no app_token: {data!r}")
        created = True

    table_id, err = await _first_table_id(app_token, user_key)
    if err is not None:
        return err

    granted: dict[str, str] = {}
    mentor_grant = await _grant(app_token, mentor_open_id.strip(), "edit", user_key, identity)
    granted["mentor"] = "ok" if mentor_grant["ok"] else mentor_grant.get("message", "failed")
    if boss_open_id.strip():
        boss_grant = await _grant(app_token, boss_open_id.strip(), "view", user_key, identity)
        granted["boss"] = "ok" if boss_grant["ok"] else boss_grant.get("message", "failed")

    return {
        "ok": True,
        "app_token": app_token,
        "table_id": table_id,
        "created": created,
        "base_name": base_name,
        "granted": granted,
        "bot_access": "not_granted",
    }
