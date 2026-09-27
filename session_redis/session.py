# Copyright 2016-2024 Camptocamp SA
# Copyright 2026 Hibou Corp.
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html)

import builtins
import json
import logging
import time
from collections.abc import Iterable
from typing import TypeAlias

from odoo.http.session import (
    SESSION_LIFETIME,
    Session,
    SessionStore,
    _session_identifier_re,
)

from . import json_encoding

# this was equal to the duration of the session garbage collector in
# odoo.http.session_gc()
DEFAULT_SESSION_TIMEOUT_ANONYMOUS = 60 * 60 * 3  # 3 hours in seconds

_logger = logging.getLogger(__name__)


# Many parts of the session store API operate not on full session keys, but only
# the first n characters of them (see odoo.http.STORED_SESSION_BYTES). In
# particular used by Devices, but Odoo in general seems to promise that this
# partial sid will be safe to store in the database, and can be used to later
# find sessions, even if those sessions are actually longer.
PartialSid: TypeAlias = str


def _to_seconds(value, default):
    if value in (None, False, ""):
        return default
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        _logger.warning("Invalid session expiration %r, using %s seconds", value, default)
        return default
    return seconds if seconds > 0 else default


class RedisSessionStore(SessionStore):
    """SessionStore that saves session to redis"""

    def __init__(
        self,
        redis,
        session_cls=Session,
        prefix="",
        expiration=None,
        anon_expiration=None,
        ):
        self.path = None
        self.session_cls = session_cls
        self.redis = redis
        self.expiration = _to_seconds(expiration, SESSION_LIFETIME)
        self.anon_expiration = _to_seconds(anon_expiration, DEFAULT_SESSION_TIMEOUT_ANONYMOUS)
        self.prefix = "session:"
        if prefix:
            self.prefix = f"{self.prefix}{prefix}:"

    def build_key(self, sid):
        return f"{self.prefix}{sid}"

    def _session_ttl(self, session):
        # A rotated session carries an absolute ``deletion_time`` (epoch); it must
        # only survive the rotation window (odoo.http.SESSION_DELETION_TIMER).
        deletion_time = session.get("deletion_time")
        if deletion_time:
            try:
                return max(1, int(float(deletion_time) - time.time()))
            except (TypeError, ValueError):
                pass
        default = self.expiration if session.uid else self.anon_expiration
        # Allow a custom relative expiration, e.g. very short monitoring sessions.
        return _to_seconds(session.get("expiration"), default)

    def save(self, session):
        key = self.build_key(session.sid)
        expiration = self._session_ttl(session)
        if _logger.isEnabledFor(logging.DEBUG):
            if session.uid:
                user_msg = f"user '{session.login}' (id: {session.uid})"
            else:
                user_msg = "anonymous user"
            _logger.debug(
                f"saving session with key '{key}' and "
                f"expiration of {expiration} seconds for {user_msg}"
            )

        data = json.dumps(dict(session), cls=json_encoding.SessionEncoder).encode(
            "utf-8"
        )
        return self.redis.set(key, data, ex=expiration)

    def delete(self, session):
        key = self.build_key(session.sid)
        _logger.debug(f"deleting session with key {key}")
        return self.redis.delete(key)

    def _decode(self, key, saved):
        try:
            return json.loads(saved.decode("utf-8"), cls=json_encoding.SessionDecoder)
        except ValueError:
            _logger.debug(
                f"session for key '{key}' has been asked but its json "
                "content could not be read, it has been reset"
            )
            return {}

    def get(self, sid, *, keep_sid=False):
        if not self.is_valid_session_id(sid):
            _logger.debug(
                f"session with invalid sid '{sid}' has been asked, returning a new one"
            )
            return self.new()

        key = self.build_key(sid)
        saved = self.redis.get(key)
        if not saved:
            if keep_sid:
                _logger.debug(
                    f"session with non-existent key '{key}' has been asked, "
                    "returning an empty one with the same sid"
                )
                return self.session_cls({}, sid, new=False)
            _logger.debug(
                f"session with non-existent key '{key}' has been asked, "
                "returning a new one"
            )
            return self.new()
        return self.session_cls(self._decode(key, saved), sid, new=False)

    def get_many(self, sids: Iterable[str]) -> dict:
        """
        Fetch several sessions in a single round trip.

        :returns: ``{sid: Session}`` for every valid sid that exists in redis;
            missing or invalid sids are omitted.
        """
        sids = [sid for sid in dict.fromkeys(sids) if sid and self.is_valid_session_id(sid)]
        if not sids:
            return {}
        keys = [self.build_key(sid) for sid in sids]
        # RedisCluster cannot MGET keys living in different hash slots.
        mget = getattr(self.redis, "mget_nonatomic", None) or self.redis.mget
        result = {}
        for sid, key, saved in zip(sids, keys, mget(keys)):
            if saved:
                result[sid] = self.session_cls(self._decode(key, saved), sid, new=False)
        return result

    def list(self):
        keys = self.redis.keys(f"{self.prefix}*")
        _logger.debug("a listing redis keys has been called")
        return [key[len(self.prefix) :] for key in keys]

    def vacuum(self, *args, **kwargs):
        """Do not garbage collect the sessions

        Redis keys are automatically cleaned at the end of their
        expiration.
        """
        return None

    def delete_old_sessions(self, session):
        """
        # Deletion of rotated sessions is handled by updating the sessions'
        # expiry based on deletion_time in save(), so this method is redundant
        # when using a redis store.

        # While this method is not part of the generic SessionStore API, it is
        # defined on the file session store, and is used by the Session itself
        # as part of the session rotation (see odoo.http.Session._delete_old_sessions).
        """
        return

    def get_missing_session_identifiers(
        self, identifiers: builtins.list[PartialSid]
    ) -> set[PartialSid]:
        """
        Given a list of partial session ids, return a set of those session ids
        which no longer exist in the keystore.

        While this method is not part of the generic SessionStore API, it is
        defined on the file session store, and is used by Odoo's devices to
        figure out what needs to be revoked
        (see odoo.addons.base.models.res_device.ResDeviceLog.__update_revoked).
        """
        identifiers = set(identifiers)
        not_found = set()
        for partial_sid in identifiers:
            key = f"{self.prefix}{partial_sid}*"
            match = next(self.redis.scan_iter(match=key), None)
            if not match:
                not_found.add(partial_sid)
        return not_found

    def delete_from_identifiers(self, identifiers: builtins.list[PartialSid]):
        """
        Given a list of partial session ids, remove any that are in the session store.

        While this method is not part of the generic SessionStore API, it is
        defined on the file session store, and is used by devices when revoking
        device sessions (see odoo.addons.base.models.res_device.ResDevice._revoke).
        """
        patterns_to_unlink = []
        for identifier in identifiers:
            # Avoid removing a session if it does not match an identifier. See this same
            # comment in odoo.http.FileSessionStore.delete_from_identifiers.
            if not _session_identifier_re.match(identifier):
                raise ValueError(
                    "Identifier format incorrect, did you pass in a string instead ",
                    "of a list?",
                )
            patterns_to_unlink.append(f"{self.prefix}{identifier}*")
        keys_to_unlink = []
        for pattern in patterns_to_unlink:
            keys_to_unlink.extend(self.redis.scan_iter(match=pattern))
        if keys_to_unlink:
            self.redis.delete(*keys_to_unlink)
