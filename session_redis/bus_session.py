# Copyright 2026 Hibou Corp.
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html)
"""
Redis aware replacement for ``odoo.addons.bus.session_helpers.check_sessions``.

The upstream helper validates websocket sessions by opening the session file
(``store.get_session_path``) to compare its mtime/inode, which only makes sense
for the filesystem store. With ``RedisSessionStore`` the store has no path and
every bus worker loop crashed with ``TypeError: expected str ... not NoneType``.
"""

import hashlib
import hmac
import logging
import time

from odoo.http import session as http_session
from odoo.sql_db import SQL
from odoo.tools.misc import consteq

from .session import RedisSessionStore

_logger = logging.getLogger(__name__)

_original_check_sessions = None


def _check_redis_sessions(cr, sessions, store):
    from odoo.addons.bus.session_helpers import _get_session_token_query_params

    now = time.time()
    resolved_by_sid = {}
    pending_by_sid = {}
    for stored_session in sessions:
        sid = stored_session.sid
        if sid in resolved_by_sid or sid in pending_by_sid:
            continue
        if stored_session.uid is None:
            resolved_by_sid[sid] = stored_session  # No user, no token to match.
            continue
        pending_by_sid[sid] = stored_session
    if not pending_by_sid:
        return resolved_by_sid

    stored_by_sid = store.get_many(pending_by_sid)
    next_sid_by_sid = {
        sid: stored["next_sid"]
        for sid, stored in stored_by_sid.items()
        if "next_sid" in stored
    }
    if next_sid_by_sid:
        rotated = store.get_many(next_sid_by_sid.values())
        for sid, next_sid in next_sid_by_sid.items():
            if next_sid in rotated:
                stored_by_sid[sid] = rotated[next_sid]
            else:
                stored_by_sid.pop(sid, None)

    to_check_by_sid = {
        sid: stored
        for sid, stored in stored_by_sid.items()
        if stored.uid is not None
        and not ("deletion_time" in stored and stored["deletion_time"] <= now)
    }
    if not to_check_by_sid:
        return resolved_by_sid

    uids = {session.uid for session in to_check_by_sid.values()}
    query_params = _get_session_token_query_params(cr, uids)
    cr.execute(
        SQL(
            "SELECT %(select)s FROM %(from)s %(joins)s WHERE %(where)s GROUP BY %(group_by)s",
            **query_params,
        ),
    )
    id_idx = next(i for i, column in enumerate(cr.description) if column.name == "id")
    keys_by_uid = {}
    # Mirror `_session_token_get_values` then `_session_token_hash_compute`.
    for row in cr.fetchall():
        field_values = tuple(
            (column.name, row[index]) for index, column in enumerate(cr.description)
        )
        key_tuple = tuple((k, v) for k, v in field_values if v is not None)
        keys_by_uid[row[id_idx]] = str(key_tuple).encode()

    for given_sid, stored_session in to_check_by_sid.items():
        key = keys_by_uid.get(stored_session.uid)
        if not key or not stored_session.session_token:
            continue
        token = hmac.new(key, stored_session.sid.encode(), hashlib.sha256).hexdigest()
        if consteq(token, stored_session.session_token):
            resolved_by_sid[given_sid] = stored_session
    return resolved_by_sid


def check_sessions(cr, sessions):
    store = http_session.session_store()
    if isinstance(store, RedisSessionStore):
        return _check_redis_sessions(cr, sessions, store)
    return _original_check_sessions(cr, sessions)


def patch_bus_check_sessions():
    """Swap the bus helper, including the name bound in ``bus_dispatcher``."""
    global _original_check_sessions
    try:
        from odoo.addons.bus import bus_dispatcher, session_helpers
    except ImportError:
        _logger.debug("bus is not available, websocket session check not patched")
        return
    if session_helpers.check_sessions is check_sessions:
        return
    _original_check_sessions = session_helpers.check_sessions
    session_helpers.check_sessions = check_sessions
    bus_dispatcher.check_sessions = check_sessions
