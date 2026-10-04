"""Pocketful stage 4: payments, holds, UI, statements, corrections, refunds and batches.

Single-process HTTP service built on the Python standard library. All state lives in
memory in one `State` object; every read and write runs under one global lock, which
makes debits/credits atomic and idempotent writes exactly-once under concurrency.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

TRACK = "pocketful"
FORMAT_VERSION = 1

MAX_AMOUNT = 1_000_000_000
MAX_NOTE = 200
MAX_KEY = 255
MAX_BODY = 8 * 1024 * 1024
MAX_TRANSFERS = 32
DEFAULT_AUTH_TTL = 600
AUTH_STATUSES = ("open", "captured", "voided", "expired")
HANDLE_RE = re.compile(r"[a-z0-9_]{1,20}")
DIGITS_RE = re.compile(r"[0-9]+")
STATUSES = ("pending", "paid", "declined", "cancelled")
VISIBILITIES = ("public", "private")

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
# Seeded fixture users are hashed with a lighter scrypt cost so that a reset with many
# distinct passwords stays well inside its 10-second budget.
SEED_SCRYPT_N = 2 ** 11


# ---------------------------------------------------------------------------
# errors and small helpers


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code.replace("_", " ")


def malformed(msg="malformed request"):
    return ApiError(400, "malformed_request", msg)


def invalid(msg="validation failed"):
    return ApiError(422, "validation_failed", msg)


def not_found(msg="not found"):
    return ApiError(404, "not_found", msg)


def forbidden(msg="forbidden"):
    return ApiError(403, "forbidden", msg)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


RFC3339_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|z|[+-]\d{2}:\d{2})")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
ONE_US = timedelta(microseconds=1)


def now_us() -> int:
    return time.time_ns() // 1000


def fmt_us(us: int) -> str:
    """RFC 3339 in UTC with microseconds, from integer microseconds."""
    dt = EPOCH + timedelta(microseconds=us)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "+00:00"


def parse_us(value) -> int:
    """Parse an RFC 3339 instant with an explicit offset into integer microseconds.

    Raises ValueError for anything else (naive times, bare dates, empty strings).
    """
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    m = RFC3339_RE.fullmatch(value)
    if not m:
        raise ValueError("not an RFC 3339 instant with offset")
    y, mo, d, h, mi, sec = (int(x) for x in m.groups()[:6])
    frac = (m.group(7) or "")[:6].ljust(6, "0")
    off = m.group(8)
    if off in ("Z", "z"):
        tz = timezone.utc
    else:
        oh, om = int(off[1:3]), int(off[4:6])
        if oh > 23 or om > 59:
            raise ValueError("bad offset")
        delta = timedelta(hours=oh, minutes=om)
        tz = timezone(delta if off[0] == "+" else -delta)
    dt = datetime(y, mo, d, h, mi, sec, int(frac), tzinfo=tz)
    return (dt - EPOCH) // ONE_US


def is_int_value(v) -> bool:
    """An integral JSON number (1000, 1000.0, 1e3); never a bool or string."""
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    if isinstance(v, float):
        return math.isfinite(v) and v.is_integer()
    return False


def canon(v):
    """Normalise a parsed JSON value so equal JSON values compare equal."""
    if isinstance(v, dict):
        return {k: canon(x) for k, x in v.items()}
    if isinstance(v, list):
        return [canon(x) for x in v]
    if isinstance(v, float) and math.isfinite(v) and v.is_integer():
        return int(v)
    return v


def canon_text(v) -> str:
    return json.dumps(canon(v), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _reject_constant(name):
    raise ValueError(f"invalid JSON constant {name}")


def parse_json(raw: bytes):
    try:
        text = raw.decode("utf-8")
        if text.startswith("﻿"):
            text = text[1:]
        return json.loads(text, parse_constant=_reject_constant)
    except Exception:
        raise malformed("body is not valid JSON") from None


def to_amount(v, *, allow_zero=False) -> int:
    if not is_int_value(v):
        raise invalid("amount must be an integer")
    n = int(v)
    if n < (0 if allow_zero else 1) or n > MAX_AMOUNT:
        raise invalid("amount out of range")
    return n


def opt_note(body: dict) -> str:
    if "note" not in body:
        return ""
    note = body["note"]
    if not isinstance(note, str):
        raise invalid("note must be a string")
    if len(note) > MAX_NOTE:
        raise invalid("note longer than 200 characters")
    return note


def opt_visibility(body: dict) -> str:
    if "visibility" not in body:
        return "public"
    vis = body["visibility"]
    if vis not in VISIBILITIES or not isinstance(vis, str):
        raise invalid("visibility must be public or private")
    return vis


def req_string(body: dict, field: str) -> str:
    if field not in body or body[field] is None:
        raise invalid(f"{field} is required")
    value = body[field]
    if not isinstance(value, str):
        raise malformed(f"{field} must be a string")
    return value


# ---------------------------------------------------------------------------
# passwords

_VERIFY_KEY = secrets.token_bytes(32)
_VERIFIED: dict[str, bytes] = {}
_VERIFIED_LOCK = threading.Lock()


def hash_password(password: str, n: int = SCRYPT_N) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8", "surrogatepass"), salt=salt,
                        n=n, r=SCRYPT_R, p=SCRYPT_P, maxmem=64 * 1024 * 1024)
    return f"scrypt${n}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    pw = password.encode("utf-8", "surrogatepass")
    # In-process memo of a keyed MAC of the last verified password per stored hash, so
    # repeated logins do not pay the scrypt cost each time. Never exported.
    tag = hmac.new(_VERIFY_KEY, stored.encode() + b"\0" + pw, hashlib.sha256).digest()
    with _VERIFIED_LOCK:
        known = _VERIFIED.get(stored)
    if known is not None and hmac.compare_digest(known, tag):
        return True
    try:
        algo, n, r, p, salt_hex, dk_hex = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(pw, salt=bytes.fromhex(salt_hex), n=int(n), r=int(r),
                            p=int(p), maxmem=64 * 1024 * 1024)
    except Exception:
        return False
    ok = hmac.compare_digest(dk, bytes.fromhex(dk_hex))
    if ok:
        with _VERIFIED_LOCK:
            if len(_VERIFIED) > 100_000:
                _VERIFIED.clear()
            _VERIFIED[stored] = tag
    return ok


def check_hash_format(stored) -> None:
    if not isinstance(stored, str):
        raise ValueError("password hash must be a string")
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        raise ValueError("bad password hash")
    n, r, p = (int(x) for x in parts[1:4])
    if n < 2 or n > 2 ** 20 or n & (n - 1) or not 1 <= r <= 32 or not 1 <= p <= 16:
        raise ValueError("bad scrypt parameters")
    bytes.fromhex(parts[4])
    bytes.fromhex(parts[5])


# ---------------------------------------------------------------------------
# state


class State:
    """All service state. Times are integer microseconds since the Unix epoch.

    Ledger model: every user has an `opening` balance; every payment has an
    append-only list of revisions (amount, effective time, recorded time). A
    user's balance in any view is the opening balance plus the selected revision
    of each of their payments, applied by effective time. `balance` on the user
    record is the current (latest-revision) value, kept incrementally.
    """

    def __init__(self, currency: str, minor_units: int):
        self.currency = currency
        self.minor_units = minor_units
        self.users: dict[str, dict] = {}       # id -> user
        self.by_email: dict[str, str] = {}     # lowercased email -> id
        self.by_handle: dict[str, str] = {}    # handle -> id
        self.tokens: dict[str, str] = {}       # token -> user id
        self.payments: dict[str, dict] = {}    # id -> payment (insertion order = seq)
        self.user_payments: dict[str, list] = {}   # user id -> payment ids
        self.requests: dict[str, dict] = {}
        self.splits: dict[str, dict] = {}
        self.settlements: dict[str, dict] = {}
        self.operators: set[str] = set()
        self.idem: dict[str, dict] = {}        # scope key -> record
        self.auths: dict[str, dict] = {}       # id -> authorization
        self.user_auths: dict[str, list] = {}  # payer id -> authorization ids
        self.open_auths: set[str] = set()      # ids whose stored status is open
        self.snapshots: dict[str, dict] = {}   # statement snapshot token -> frozen result
        self.batches: dict[str, dict] = {}     # correction batch id -> summary
        self.auth_ttl = DEFAULT_AUTH_TTL
        self.seq = 0
        self.last_ts = 0

    # -- clock and sequencing ------------------------------------------------
    def now(self) -> int:
        """A strictly increasing service clock in microseconds."""
        ts = max(now_us(), self.last_ts + 1)
        self.last_ts = ts
        return ts

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    # -- holds ---------------------------------------------------------------
    def expire_holds(self, now=None) -> None:
        """Close every open authorization whose deadline has passed (lazy expiry)."""
        now = self.now() if now is None else now
        for aid in [a for a in self.open_auths if self.auths[a]["expires_ts"] <= now]:
            a = self.auths[aid]
            a["status"] = "expired"
            a["closed_ts"] = a["expires_ts"]
            a["close_kind"] = "expired"
            self.open_auths.discard(aid)

    def held(self, uid: str) -> int:
        return sum(a["amount"] - a["captured_amount"]
                   for a in (self.auths[x] for x in self.open_auths)
                   if a["from_user_id"] == uid)

    def available(self, user: dict) -> int:
        return user["balance"] - self.held(user["id"])

    def captures(self, a: dict) -> list:
        out = []
        for pid in a["payment_ids"]:
            p = self.payments.get(pid)
            if p is not None:
                out.append((p["ts"], p["revisions"][0]["amount"]))
        return out

    # -- users ---------------------------------------------------------------
    def add_user(self, uid, email, pw_hash, display_name, handle, balance, opening=None):
        self.users[uid] = {"id": uid, "email": email, "password_hash": pw_hash,
                           "display_name": display_name, "handle": handle,
                           "balance": balance,
                           "opening": balance if opening is None else opening}
        self.by_email[email.lower()] = uid
        self.by_handle[handle] = uid
        self.user_payments.setdefault(uid, [])
        self.user_auths.setdefault(uid, [])

    def user_by_handle(self, handle):
        uid = self.by_handle.get(handle) if isinstance(handle, str) else None
        return self.users.get(uid) if uid else None

    def index_payment(self, p: dict) -> None:
        p.setdefault("refund_of", None)
        p.setdefault("refund_ids", [])
        self.payments[p["id"]] = p
        self.user_payments.setdefault(p["from_user_id"], []).append(p["id"])
        self.user_payments.setdefault(p["to_user_id"], []).append(p["id"])

    def refunded(self, p: dict) -> int:
        """Total already refunded against a payment."""
        return sum(self.payments[r]["amount"] for r in p["refund_ids"] if r in self.payments)

    @staticmethod
    def immutable(p: dict) -> bool:
        """Captures and refunds can never be corrected."""
        return p.get("authorization_id") is not None or p.get("refund_of") is not None

    def index_auth(self, a: dict) -> None:
        self.auths[a["id"]] = a
        self.user_auths.setdefault(a["from_user_id"], []).append(a["id"])
        if a["status"] == "open":
            self.open_auths.add(a["id"])

    # -- views ---------------------------------------------------------------
    def payment_view(self, p: dict, amount=None) -> dict:
        frm, to = self.users[p["from_user_id"]], self.users[p["to_user_id"]]
        return {
            "payment_id": p["id"],
            "from_user_id": frm["id"],
            "from_handle": frm["handle"],
            "to_user_id": to["id"],
            "to_handle": to["handle"],
            "amount": p["amount"] if amount is None else amount,
            "currency": self.currency,
            "note": p["note"],
            "visibility": p["visibility"],
            "request_id": p["request_id"],
            "settlement_id": p["settlement_id"],
            "authorization_id": p.get("authorization_id"),
            "refund_of": p.get("refund_of"),
            "created_at": p["created_at"],
        }

    def auth_view(self, a: dict) -> dict:
        frm, to = self.users[a["from_user_id"]], self.users[a["to_user_id"]]
        is_open = a["status"] == "open"
        return {
            "authorization_id": a["id"],
            "from_user_id": frm["id"],
            "from_handle": frm["handle"],
            "to_user_id": to["id"],
            "to_handle": to["handle"],
            "amount": a["amount"],
            "captured_amount": a["captured_amount"],
            "remaining_amount": a["amount"] - a["captured_amount"] if is_open else 0,
            "currency": self.currency,
            "note": a["note"],
            "visibility": a["visibility"],
            "status": a["status"],
            "expires_at": a["expires_at"],
            "payment_id": a["payment_ids"][-1] if a["payment_ids"] else a.get("payment_id"),
            "payment_ids": list(a["payment_ids"]),
            "created_at": a["created_at"],
            "closed_at": None if is_open or a.get("closed_ts") is None else (
                a["expires_at"] if a.get("close_kind") == "expired" else fmt_us(a["closed_ts"])),
        }

    def request_view(self, r: dict) -> dict:
        req, payer = self.users[r["requester_id"]], self.users[r["payer_id"]]
        return {
            "request_id": r["id"],
            "requester_id": req["id"],
            "requester_handle": req["handle"],
            "payer_id": payer["id"],
            "payer_handle": payer["handle"],
            "amount": r["amount"],
            "currency": self.currency,
            "note": r["note"],
            "status": r["status"],
            "payment_id": r["payment_id"],
            "created_at": r["created_at"],
        }

    @staticmethod
    def revision_view(p: dict, rev: dict) -> dict:
        return {"payment_id": p["id"], "revision": rev["revision"], "amount": rev["amount"],
                "effective_at": rev["effective_at"], "recorded_at": rev["recorded_at"],
                "reason": rev["reason"], "correction_batch_id": rev.get("correction_batch_id")}

    # -- money ---------------------------------------------------------------
    def new_payment_record(self, frm, to, amount, note, visibility, ts, *, request_id=None,
                           settlement_id=None, authorization_id=None, refund_of=None) -> dict:
        stamp = fmt_us(ts)
        p = {"id": new_id("p"), "from_user_id": frm["id"], "to_user_id": to["id"],
             "amount": amount, "note": note, "visibility": visibility,
             "request_id": request_id, "settlement_id": settlement_id,
             "authorization_id": authorization_id, "refund_of": refund_of,
             "created_at": stamp, "ts": ts, "seq": self.next_seq(),
             "revisions": [{"revision": 1, "amount": amount, "effective_at": stamp,
                            "effective_ts": ts, "recorded_at": stamp, "recorded_ts": ts,
                            "reason": ""}]}
        self.index_payment(p)
        return p

    def make_payment(self, frm: dict, to: dict, amount: int, note: str, visibility: str,
                     *, request_id=None, settlement_id=None, authorization_id=None,
                     ts=None) -> dict:
        # A capture spends money already reserved for it; anything else needs
        # unreserved (available) funds.
        if authorization_id is None and self.available(frm) < amount:
            raise ApiError(409, "insufficient_funds", "available balance is below amount")
        ts = self.now() if ts is None else ts
        frm["balance"] -= amount
        to["balance"] += amount
        return self.new_payment_record(frm, to, amount, note, visibility, ts,
                                       request_id=request_id, settlement_id=settlement_id,
                                       authorization_id=authorization_id)

    def make_request(self, requester: dict, payer: dict, amount: int, note: str,
                     ts=None) -> dict:
        ts = self.now() if ts is None else ts
        r = {"id": new_id("rq"), "requester_id": requester["id"], "payer_id": payer["id"],
             "amount": amount, "note": note, "status": "pending", "payment_id": None,
             "created_at": fmt_us(ts), "ts": ts, "seq": self.next_seq()}
        self.requests[r["id"]] = r
        return r

    # -- ledger views --------------------------------------------------------
    @staticmethod
    def selected_revision(p: dict, known: int):
        """The latest revision recorded at or before `known`, or None."""
        for rev in reversed(p["revisions"]):
            if rev["recorded_ts"] <= known:
                return rev
        return None

    def signed(self, p: dict, uid: str, amount: int) -> int:
        if p["from_user_id"] == uid:
            return -amount
        return amount

    def balance_view(self, uid: str, as_of: int, known: int) -> int:
        total = self.users[uid]["opening"]
        for pid in self.user_payments.get(uid, ()):
            p = self.payments[pid]
            rev = self.selected_revision(p, known)
            if rev is not None and rev["effective_ts"] <= as_of:
                total += self.signed(p, uid, rev["amount"])
        return total

    def hold_view(self, a: dict, as_of: int, known: int) -> int:
        created = a["ts"]
        if created > as_of or created > known:
            return 0
        if a["expires_ts"] <= as_of:
            return 0
        limit = min(as_of, known)
        closed = a.get("closed_ts")
        if closed is not None and a.get("close_kind") != "expired" and closed <= limit:
            return 0
        held = a["initial_held"] - sum(x for t, x in self.captures(a) if t <= limit)
        return max(held, 0)

    def held_view(self, uid: str, as_of: int, known: int) -> int:
        return sum(self.hold_view(self.auths[aid], as_of, known)
                   for aid in self.user_auths.get(uid, ()))

    def timeline(self, uid: str, override=None) -> list:
        """(time, d_total, d_held) events under the latest revisions."""
        events = []
        for pid in self.user_payments.get(uid, ()):
            p = self.payments[pid]
            rev = override.get(pid) if override and pid in override else p["revisions"][-1]
            events.append((rev["effective_ts"], self.signed(p, uid, rev["amount"]), 0))
        for aid in self.user_auths.get(uid, ()):
            a = self.auths[aid]
            if a["initial_held"] <= 0:
                continue
            caps = self.captures(a)
            events.append((a["ts"], 0, a["initial_held"]))
            closed = a.get("closed_ts")
            if closed is not None and a.get("close_kind") != "expired":
                end = closed
            else:
                end = a["expires_ts"]
            taken = 0
            for t, x in caps:
                if t <= end:
                    events.append((t, 0, -x))
                    taken += x
            remaining = a["initial_held"] - taken
            if remaining > 0:
                events.append((end, 0, -remaining))
        return events

    def sweep(self, uid: str, events: list, times: list) -> list:
        """Total and available just after all events at each of the given times."""
        events = sorted(events)
        out, i = [], 0
        total, held = self.users[uid]["opening"], 0
        for t in times:
            while i < len(events) and events[i][0] <= t:
                total += events[i][1]
                held += events[i][2]
                i += 1
            out.append((total, total - held))
        return out

    def correction_overdrafts(self, uid: str, override: dict) -> bool:
        """True if applying `override` makes total or available negative (and lower)
        at some effective/event boundary that it affects."""
        before = self.timeline(uid)
        after = self.timeline(uid, override)
        times = sorted({e[0] for e in before} | {e[0] for e in after})
        b = self.sweep(uid, before, times)
        a = self.sweep(uid, after, times)
        for (bt, ba), (at, aa) in zip(b, a):
            if (at < 0 and at < bt) or (aa < 0 and aa < ba):
                return True
        return False

    # -- serialisation -------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "schema": 3,
            "currency": self.currency,
            "minor_units": self.minor_units,
            "users": list(self.users.values()),
            "tokens": self.tokens,
            "payments": list(self.payments.values()),
            "requests": list(self.requests.values()),
            "splits": list(self.splits.values()),
            "settlements": list(self.settlements.values()),
            "operators": sorted(self.operators),
            "idempotency": list(self.idem.values()),
            "authorizations": list(self.auths.values()),
            "authorization_ttl_seconds": self.auth_ttl,
            "snapshots": self.snapshots,
            "correction_batches": self.batches,
            "seq": self.seq,
            "last_ts": self.last_ts,
        }

    @classmethod
    def from_dict(cls, d) -> "State":
        """Rebuild exported state (stage 1, 2 or 3 format), validating it strictly.

        Older formats carry no ledger: revisions and opening balances are derived
        from the stored payments, and hold lifecycles from the authorizations and
        their capture payments. Times are always re-derived from the RFC 3339 strings.
        """
        def need(cond, msg="invalid state"):
            if not cond:
                raise ValueError(msg)

        def s(v):
            need(isinstance(v, str))
            return v

        def i(v):
            need(isinstance(v, int) and not isinstance(v, bool))
            return v

        def opt_s(v):
            need(v is None or isinstance(v, str))
            return v

        need(isinstance(d, dict))
        currency = s(d["currency"])
        mu = i(d["minor_units"])
        need(mu in (0, 2, 3))
        st = cls(currency, mu)
        openings = {}
        for u in d["users"]:
            need(isinstance(u, dict))
            uid, email, handle = s(u["id"]), s(u["email"]), s(u["handle"])
            need(0 < len(uid) <= 64 and HANDLE_RE.fullmatch(handle) is not None)
            need(uid not in st.users and handle not in st.by_handle
                 and email.lower() not in st.by_email)
            check_hash_format(u["password_hash"])
            bal = i(u["balance"])
            need(0 <= bal <= 2 ** 53)
            if "opening" in u:
                openings[uid] = i(u["opening"])
            st.add_user(uid, email, u["password_hash"], s(u["display_name"]), handle, bal)
        need(isinstance(d["tokens"], dict))
        for tok, uid in d["tokens"].items():
            need(isinstance(uid, str) and uid in st.users and len(tok) > 0)
            st.tokens[tok] = uid
        for p in d["payments"]:
            need(isinstance(p, dict))
            pid = s(p["id"])
            need(0 < len(pid) <= 64 and pid not in st.payments)
            need(s(p["from_user_id"]) in st.users and s(p["to_user_id"]) in st.users)
            need(i(p["amount"]) >= 0)
            s(p["note"])
            need(p["visibility"] in VISIBILITIES)
            opt_s(p["request_id"])
            opt_s(p["settlement_id"])
            opt_s(p.get("authorization_id"))
            ts = parse_us(p["created_at"])
            revs = p.get("revisions")
            if revs is None:
                revs = [{"revision": 1, "amount": p["amount"], "effective_at": p["created_at"],
                         "effective_ts": ts, "recorded_at": p["created_at"], "recorded_ts": ts,
                         "reason": ""}]
            need(isinstance(revs, list) and len(revs) >= 1)
            clean = []
            for n, rev in enumerate(revs, start=1):
                need(isinstance(rev, dict) and i(rev["revision"]) == n)
                need(0 <= i(rev["amount"]) <= MAX_AMOUNT or n == 1)
                s(rev["reason"])
                clean.append({"revision": n, "amount": rev["amount"],
                              "effective_at": s(rev["effective_at"]),
                              "effective_ts": parse_us(rev["effective_at"]),
                              "recorded_at": s(rev["recorded_at"]),
                              "recorded_ts": parse_us(rev["recorded_at"]),
                              "reason": rev["reason"],
                              "correction_batch_id": opt_s(rev.get("correction_batch_id"))})
            need(clean[0]["amount"] == p["amount"])
            st.index_payment({
                "id": pid, "from_user_id": p["from_user_id"], "to_user_id": p["to_user_id"],
                "amount": p["amount"], "note": p["note"], "visibility": p["visibility"],
                "request_id": p["request_id"], "settlement_id": p["settlement_id"],
                "authorization_id": p.get("authorization_id"),
                "refund_of": opt_s(p.get("refund_of")),
                "created_at": p["created_at"], "ts": ts,
                "seq": i(p["seq"]) if "seq" in p else st.next_seq(), "revisions": clean})
        for p in st.payments.values():
            if p["refund_of"] is not None:
                need(p["refund_of"] in st.payments)
                st.payments[p["refund_of"]]["refund_ids"].append(p["id"])
        batches = d.get("correction_batches", {})
        need(isinstance(batches, dict))
        st.batches = batches
        for uid, u in st.users.items():
            if uid in openings:
                u["opening"] = openings[uid]
            else:
                net = sum(st.signed(st.payments[pid], uid, st.payments[pid]["revisions"][-1]["amount"])
                          for pid in st.user_payments[uid])
                u["opening"] = u["balance"] - net
        for r in d["requests"]:
            need(isinstance(r, dict))
            rid = s(r["id"])
            need(0 < len(rid) <= 64 and rid not in st.requests)
            need(s(r["requester_id"]) in st.users and s(r["payer_id"]) in st.users)
            need(i(r["amount"]) >= 0)
            s(r["note"])
            need(r["status"] in STATUSES)
            opt_s(r["payment_id"])
            st.requests[rid] = {
                "id": rid, "requester_id": r["requester_id"], "payer_id": r["payer_id"],
                "amount": r["amount"], "note": r["note"], "status": r["status"],
                "payment_id": r["payment_id"], "created_at": s(r["created_at"]),
                "ts": parse_us(r["created_at"]),
                "seq": i(r["seq"]) if "seq" in r else st.next_seq()}
        for sp in d["splits"]:
            need(isinstance(sp, dict) and isinstance(sp.get("id"), str))
            st.splits[sp["id"]] = sp
        for se in d["settlements"]:
            need(isinstance(se, dict) and isinstance(se.get("id"), str))
            need(isinstance(se.get("payment_ids"), list))
            need(all(pid in st.payments for pid in se["payment_ids"]))
            st.settlements[se["id"]] = se
        for op in d["operators"]:
            need(isinstance(op, str))
            st.operators.add(op)
        for rec in d["idempotency"]:
            need(isinstance(rec, dict))
            need(s(rec["user_id"]) in st.users)
            s(rec["method"]), s(rec["path"]), s(rec["key"]), s(rec["body"])
            need(isinstance(rec["status"], int) and isinstance(rec["response"], (dict, list)))
            st.idem[idem_scope(rec["user_id"], rec["method"], rec["path"], rec["key"])] = rec
        # Stage-1 exports carry no authorizations and no TTL; both default.
        ttl = d.get("authorization_ttl_seconds", DEFAULT_AUTH_TTL)
        need(i(ttl) > 0)
        st.auth_ttl = ttl
        for a in d.get("authorizations", []):
            need(isinstance(a, dict))
            aid = s(a["id"])
            need(0 < len(aid) <= 64 and aid not in st.auths)
            need(s(a["from_user_id"]) in st.users and s(a["to_user_id"]) in st.users)
            need(1 <= i(a["amount"]) and 0 <= i(a["captured_amount"]) <= a["amount"])
            s(a["note"])
            need(a["visibility"] in VISIBILITIES and a["status"] in AUTH_STATUSES)
            need(isinstance(a["payment_ids"], list)
                 and all(x in st.payments for x in a["payment_ids"]))
            opt_s(a.get("payment_id"))
            created = parse_us(a["created_at"])
            expires = parse_us(a["expires_at"])
            rec = {
                "id": aid, "from_user_id": a["from_user_id"], "to_user_id": a["to_user_id"],
                "amount": a["amount"], "captured_amount": a["captured_amount"],
                "note": a["note"], "visibility": a["visibility"], "status": a["status"],
                "expires_at": a["expires_at"], "expires_ts": expires,
                "payment_ids": list(a["payment_ids"]), "payment_id": a.get("payment_id"),
                "created_at": a["created_at"], "ts": created,
                "seq": i(a["seq"]) if "seq" in a else st.next_seq()}
            caps = [(st.payments[x]["ts"], st.payments[x]["amount"]) for x in rec["payment_ids"]]
            if "initial_held" in a:
                rec["initial_held"] = i(a["initial_held"])
                rec["closed_ts"] = None if a.get("closed_ts") is None else i(a["closed_ts"])
                rec["close_kind"] = opt_s(a.get("close_kind"))
            else:
                # Derive the lifecycle from a stage-2 record.
                rec["initial_held"] = a["amount"] - a["captured_amount"] + sum(x for _, x in caps)
                last = max([created] + [t for t, _ in caps])
                if a["status"] == "open":
                    rec["closed_ts"], rec["close_kind"] = None, None
                elif a["status"] == "expired":
                    rec["closed_ts"], rec["close_kind"] = expires, "expired"
                else:
                    rec["closed_ts"], rec["close_kind"] = last, a["status"]
            st.index_auth(rec)
        for uid, u in st.users.items():
            need(st.held(uid) <= u["balance"], "holds exceed balance")
        snaps = d.get("snapshots", {})
        need(isinstance(snaps, dict))
        for tok, snap in snaps.items():
            need(isinstance(snap, dict) and snap.get("user_id") in st.users)
            need(isinstance(snap.get("entries"), list))
            st.snapshots[tok] = snap
        st.seq = max([i(d["seq"])] + [x["seq"] for x in st.payments.values()]
                     + [x["seq"] for x in st.requests.values()]
                     + [x["seq"] for x in st.auths.values()])
        last = d.get("last_ts")
        stamps = ([x["ts"] for x in st.payments.values()]
                  + [r["recorded_ts"] for x in st.payments.values() for r in x["revisions"]]
                  + [x["ts"] for x in st.requests.values()]
                  + [x["ts"] for x in st.auths.values()]
                  + [x["closed_ts"] for x in st.auths.values()
                     if x["closed_ts"] is not None and x["close_kind"] != "expired"])
        if isinstance(last, int) and not isinstance(last, bool):
            stamps.append(last)
        elif isinstance(last, float) and math.isfinite(last):
            stamps.append(int(last * 1_000_000))
        st.last_ts = max(stamps, default=0)
        return st


def idem_scope(user_id, method, path, key) -> str:
    return json.dumps([user_id, method, path, key])


def state_from_fixture(fx) -> State:
    """Validate a reset fixture and build state. Raises ApiError on invalid input."""
    if not isinstance(fx, dict):
        raise malformed("fixture must be a JSON object")

    def need(cond, msg):
        if not cond:
            raise invalid(msg)

    currency = fx.get("currency")
    need(isinstance(currency, str) and currency, "currency is required")
    mu = fx.get("minor_units")
    need(is_int_value(mu) and int(mu) in (0, 2, 3), "minor_units must be 0, 2 or 3")
    users = fx.get("users", [])
    payments = fx.get("payments", []) or []
    requests = fx.get("requests", []) or []
    operators = fx.get("settlement_operator_ids", []) or []
    need(isinstance(users, list), "users must be an array")
    need(isinstance(payments, list), "payments must be an array")
    need(isinstance(requests, list), "requests must be an array")
    need(isinstance(operators, list), "settlement_operator_ids must be an array")

    st = State(currency, int(mu))
    base = now_us()
    st.last_ts = base

    for u in users:
        need(isinstance(u, dict), "user must be an object")
        for f in ("id", "email", "password", "display_name", "handle"):
            need(isinstance(u.get(f), str), f"user {f} must be a string")
        need(0 < len(u["id"]) <= 64, "user id must be 1..64 characters")
        need(HANDLE_RE.fullmatch(u["handle"]) is not None, "invalid handle")
        need(is_int_value(u.get("balance")), "balance must be an integer")
        bal = int(u["balance"])
        need(bal >= 0, "balance must not be negative")
        need(bal <= 2 ** 53, "balance out of range")
        need(u["id"] not in st.users, "duplicate user id")
        need(u["handle"] not in st.by_handle, "duplicate handle")
        need(u["email"].lower() not in st.by_email, "duplicate email")
        st.add_user(u["id"], u["email"], "", u["display_name"], u["handle"], bal)

    def seeded_ts(item):
        """A supplied created_at (not in the future), or None."""
        if item.get("created_at") is None:
            return None
        try:
            ts = parse_us(item["created_at"])
        except Exception:
            raise invalid("invalid created_at") from None
        need(ts <= base, "created_at must not be in the future")
        return ts

    for p in payments:
        need(isinstance(p, dict), "payment must be an object")
        pid = p.get("id")
        need(isinstance(pid, str) and 0 < len(pid) <= 64, "payment id is required")
        need(pid not in st.payments, "duplicate payment id")
        need(p.get("from_user_id") in st.users if isinstance(p.get("from_user_id"), str)
             else False, "unknown from_user_id")
        need(p.get("to_user_id") in st.users if isinstance(p.get("to_user_id"), str)
             else False, "unknown to_user_id")
        need(is_int_value(p.get("amount")) and int(p["amount"]) >= 0, "invalid amount")
        note = p.get("note", "")
        need(isinstance(note, str), "note must be a string")
        vis = p.get("visibility", "public")
        need(vis in VISIBILITIES, "invalid visibility")
        rid = p.get("request_id")
        need(rid is None or isinstance(rid, str), "invalid request_id")
        sid = p.get("settlement_id")
        need(sid is None or isinstance(sid, str), "invalid settlement_id")
        auth_id = p.get("authorization_id")
        need(auth_id is None or isinstance(auth_id, str), "invalid authorization_id")
        ts = seeded_ts(p)
        stamp = p["created_at"] if ts is not None else fmt_us(base)
        if ts is None:
            ts = base
        amount = int(p["amount"])
        st.index_payment({
            "id": pid, "from_user_id": p["from_user_id"], "to_user_id": p["to_user_id"],
            "amount": amount, "note": note, "visibility": vis,
            "request_id": rid, "settlement_id": sid, "authorization_id": auth_id,
            "created_at": stamp, "ts": ts, "seq": st.next_seq(),
            "revisions": [{"revision": 1, "amount": amount, "effective_at": stamp,
                           "effective_ts": ts, "recorded_at": stamp, "recorded_ts": ts,
                           "reason": ""}]})

    # Seeded refunds may name an earlier seeded payment through refund_of.
    for p in payments:
        target = p.get("refund_of")
        if target is None:
            continue
        need(isinstance(target, str) and target in st.payments and target != p["id"],
             "refund_of must name a seeded payment")
        tgt, ref = st.payments[target], st.payments[p["id"]]
        need(tgt.get("refund_of") is None, "a refund cannot be refunded")
        need(ref["from_user_id"] == tgt["to_user_id"] and ref["to_user_id"] == tgt["from_user_id"],
             "a refund must reverse its target's direction")
        ref["refund_of"] = target
        tgt["refund_ids"].append(p["id"])

    # Opening balance: seeded ending balance minus the net effect of seeded payments.
    for uid, u in st.users.items():
        net = sum(st.signed(st.payments[pid], uid, st.payments[pid]["amount"])
                  for pid in st.user_payments[uid])
        u["opening"] = u["balance"] - net

    for r in requests:
        need(isinstance(r, dict), "request must be an object")
        rid = r.get("id")
        need(isinstance(rid, str) and 0 < len(rid) <= 64, "request id is required")
        need(rid not in st.requests, "duplicate request id")
        need(r.get("requester_id") in st.users if isinstance(r.get("requester_id"), str)
             else False, "unknown requester_id")
        need(r.get("payer_id") in st.users if isinstance(r.get("payer_id"), str)
             else False, "unknown payer_id")
        need(is_int_value(r.get("amount")) and int(r["amount"]) >= 0, "invalid amount")
        note = r.get("note", "")
        need(isinstance(note, str), "note must be a string")
        status = r.get("status", "pending")
        need(status in STATUSES, "invalid status")
        pay_id = r.get("payment_id")
        need(pay_id is None or isinstance(pay_id, str), "invalid payment_id")
        ts = seeded_ts(r)
        stamp = r["created_at"] if ts is not None else fmt_us(base)
        if ts is None:
            ts = base
        st.requests[rid] = {
            "id": rid, "requester_id": r["requester_id"], "payer_id": r["payer_id"],
            "amount": int(r["amount"]), "note": note, "status": status,
            "payment_id": pay_id, "created_at": stamp, "ts": ts, "seq": st.next_seq()}

    for op in operators:
        need(isinstance(op, str), "settlement_operator_ids must be strings")
        st.operators.add(op)

    if "authorization_ttl_seconds" in fx and fx["authorization_ttl_seconds"] is not None:
        ttl = fx["authorization_ttl_seconds"]
        need(is_int_value(ttl) and int(ttl) > 0,
             "authorization_ttl_seconds must be a positive integer")
        st.auth_ttl = int(ttl)
    auths = fx.get("authorizations", []) or []
    need(isinstance(auths, list), "authorizations must be an array")
    for a in auths:
        need(isinstance(a, dict), "authorization must be an object")
        aid = a.get("id")
        need(isinstance(aid, str) and 0 < len(aid) <= 64, "authorization id is required")
        need(aid not in st.auths, "duplicate authorization id")
        need(a.get("from_user_id") in st.users if isinstance(a.get("from_user_id"), str)
             else False, "unknown from_user_id")
        need(a.get("to_user_id") in st.users if isinstance(a.get("to_user_id"), str)
             else False, "unknown to_user_id")
        need(a["from_user_id"] != a["to_user_id"], "authorization to self")
        need(is_int_value(a.get("amount")) and int(a["amount"]) >= 1, "invalid amount")
        amount = int(a["amount"])
        captured = a.get("captured_amount", 0)
        captured = 0 if captured is None else captured
        need(is_int_value(captured) and 0 <= int(captured) <= amount,
             "invalid captured_amount")
        note = a.get("note", "")
        need(isinstance(note, str), "note must be a string")
        vis = a.get("visibility", "public")
        need(vis in VISIBILITIES, "invalid visibility")
        status = a.get("status", "open")
        need(status in AUTH_STATUSES, "invalid authorization status")
        try:
            expires_ts = parse_us(a.get("expires_at"))
        except Exception:
            raise invalid("expires_at must be an RFC 3339 timestamp") from None
        pay_id = a.get("payment_id")
        need(pay_id is None or isinstance(pay_id, str), "invalid payment_id")
        pay_ids = a.get("payment_ids")
        if pay_ids is None:
            pay_ids = [pay_id] if pay_id else []
        need(isinstance(pay_ids, list) and all(isinstance(x, str) for x in pay_ids),
             "invalid payment_ids")
        ts = seeded_ts(a)
        stamp = a["created_at"] if ts is not None else fmt_us(base)
        if ts is None:
            ts = base
        rec = {
            "id": aid, "from_user_id": a["from_user_id"], "to_user_id": a["to_user_id"],
            "amount": amount, "captured_amount": int(captured), "note": note,
            "visibility": vis, "status": status, "expires_at": a["expires_at"],
            "expires_ts": expires_ts, "payment_ids": list(pay_ids), "payment_id": pay_id,
            "created_at": stamp, "ts": ts, "seq": st.next_seq(),
            # Seeded closed holds do not reconstruct a lifecycle: they never held.
            "initial_held": amount - int(captured) if status == "open" else 0,
            "closed_ts": None, "close_kind": None}
        if status == "expired":
            rec["closed_ts"], rec["close_kind"] = expires_ts, "expired"
        elif status != "open":
            rec["closed_ts"], rec["close_kind"] = ts, status
        st.index_auth(rec)
    st.expire_holds(base)
    for uid, u in st.users.items():
        need(st.held(uid) <= u["balance"], "seeded open holds exceed the user's balance")

    # Hash each distinct password once, in parallel (scrypt releases the GIL).
    distinct = list(dict.fromkeys(u["password"] for u in users))
    with ThreadPoolExecutor(max_workers=4) as pool:
        hashes = dict(zip(distinct, pool.map(
            lambda pw: hash_password(pw, SEED_SCRYPT_N), distinct)))
    for u in users:
        st.users[u["id"]]["password_hash"] = hashes[u["password"]]

    st.last_ts = base
    return st


STATE = State("EUR", 2)
LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# request handling


class Ctx:
    def __init__(self, handler: "Handler", method: str, path: str, query: dict,
                 headers, raw: bytes):
        self.handler = handler
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.raw = raw

    def json_body(self, *, empty_as_object=False) -> dict:
        if not self.raw.strip():
            if empty_as_object:
                return {}
            raise malformed("request body is required")
        body = parse_json(self.raw)
        if not isinstance(body, dict):
            raise malformed("request body must be a JSON object")
        return body

    def bearer(self):
        auth = self.headers.get("Authorization")
        if not auth:
            return None
        parts = auth.strip().split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None
        return parts[1].strip()

    def user(self, st: State) -> dict:
        st.expire_holds()
        tok = self.bearer()
        uid = st.tokens.get(tok) if tok else None
        if uid is None or uid not in st.users:
            raise ApiError(401, "unauthenticated", "missing or invalid bearer token")
        return st.users[uid]

    def idem_key(self) -> str:
        key = self.headers.get("Idempotency-Key")
        if key is None or key == "":
            raise ApiError(400, "missing_idempotency_key", "Idempotency-Key is required")
        if len(key) > MAX_KEY:
            raise invalid("Idempotency-Key must be 1 to 255 characters")
        return key


def paging(query: dict) -> tuple[int, int]:
    def get_int(name, default, lo, hi):
        vals = query.get(name)
        if not vals:
            return default
        text = vals[0]
        if not DIGITS_RE.fullmatch(text):
            raise invalid(f"{name} must be a non-negative integer")
        n = int(text)
        if n < lo or (hi is not None and n > hi):
            raise invalid(f"{name} out of range")
        return n
    return get_int("limit", 50, 1, 200), get_int("offset", 0, 0, None)


def page(items: list, limit: int, offset: int) -> tuple[list, bool]:
    chunk = items[offset:offset + limit]
    return chunk, offset + limit < len(items)


def idempotent(ctx: Ctx, handler):
    """Run an idempotent write: auth, key, body, replay resolution, then the handler.

    The whole sequence runs under the global lock, so concurrent identical requests
    are serialised: the first commits and records its response, the rest replay it.
    """
    with LOCK:
        st = STATE
        user = ctx.user(st)
        if ctx.path in ("/settlements", "/correction-batches") and user["id"] not in st.operators:
            raise forbidden("settlement operator permission required")
        key = ctx.idem_key()
        body = ctx.json_body(empty_as_object=ctx.path.endswith(("/pay", "/capture")))
        scope = idem_scope(user["id"], ctx.method, ctx.path, key)
        text = canon_text(body)
        rec = st.idem.get(scope)
        if rec is not None:
            if rec["body"] != text:
                raise ApiError(409, "idempotency_key_reuse",
                               "key already used with a different body")
            return 200, copy.deepcopy(rec["response"])
        status, response = handler(st, user, body)
        st.idem[scope] = {"user_id": user["id"], "method": ctx.method, "path": ctx.path,
                          "key": key, "body": text, "status": status,
                          "response": copy.deepcopy(response)}
        return status, response


# -- auth -------------------------------------------------------------------

def valid_email(email: str) -> bool:
    if email.count("@") != 1:
        return False
    local, domain = email.split("@")
    if not local or not domain:
        return False
    return not any(c.isspace() for c in email)


def derive_handle(email: str) -> str:
    local = email.split("@", 1)[0].lower()
    return re.sub(r"[^a-z0-9_]", "_", local)[:20]


def h_signup(ctx: Ctx):
    body = ctx.json_body()
    email = req_string(body, "email")
    password = req_string(body, "password")
    display_name = req_string(body, "display_name")
    if not valid_email(email):
        raise invalid("email must be of the form local@domain")
    if len(password) < 8:
        raise invalid("password must be at least 8 characters")
    if not display_name.strip():
        raise invalid("display_name is required")
    handle = derive_handle(email)

    def conflicts(st):
        if email.lower() in st.by_email:
            raise ApiError(409, "email_taken", "email already registered")
        if handle in st.by_handle:
            raise ApiError(409, "handle_taken", "derived handle already taken")

    with LOCK:
        conflicts(STATE)
    pw_hash = hash_password(password)
    with LOCK:
        st = STATE
        conflicts(st)
        uid = new_id("u")
        st.add_user(uid, email, pw_hash, display_name, handle, 0)
        token = secrets.token_urlsafe(32)
        st.tokens[token] = uid
        return 201, {"user_id": uid, "display_name": display_name, "token": token}


def h_login(ctx: Ctx):
    body = ctx.json_body()
    email = req_string(body, "email")
    password = req_string(body, "password")
    with LOCK:
        st = STATE
        uid = st.by_email.get(email.lower())
        stored = st.users[uid]["password_hash"] if uid else None
    if stored is None or not verify_password(password, stored):
        raise ApiError(401, "unauthenticated", "wrong email or password")
    with LOCK:
        st = STATE
        user = st.users.get(uid)
        if user is None or user["password_hash"] != stored:
            raise ApiError(401, "unauthenticated", "wrong email or password")
        token = secrets.token_urlsafe(32)
        st.tokens[token] = uid
        return 200, {"user_id": uid, "display_name": user["display_name"], "token": token}


# -- wallet -----------------------------------------------------------------

def query_instant(ctx: Ctx, name: str):
    """(raw, microseconds) for an optional RFC 3339 query parameter, else (None, None)."""
    vals = ctx.query.get(name)
    if vals is None:
        return None, None
    raw = vals[0]
    try:
        return raw, parse_us(raw)
    except Exception:
        raise invalid(f"{name} must be an RFC 3339 instant with an offset") from None


def h_me(ctx: Ctx):
    with LOCK:
        st = STATE
        u = ctx.user(st)
        as_raw, as_of = query_instant(ctx, "as_of")
        known_raw, known = query_instant(ctx, "known_at")
        body = {"user_id": u["id"], "display_name": u["display_name"], "handle": u["handle"]}
        if as_raw is None and known_raw is None:
            held = st.held(u["id"])
            total = u["balance"]
        else:
            now = st.now()
            as_of = now if as_of is None else as_of
            known = now if known is None else known
            total = st.balance_view(u["id"], as_of, known)
            held = st.held_view(u["id"], as_of, known)
        body.update({"balance": total, "total": total, "available": total - held,
                     "held": held, "currency": st.currency, "minor_units": st.minor_units})
        if as_raw is not None:
            body["as_of"] = as_raw
        if known_raw is not None:
            body["known_at"] = known_raw
        return 200, body


def build_statement(st: State, uid: str, start, end: int, known: int) -> dict:
    """The full statement for [start, end) under revisions known at `known`."""
    opening = st.users[uid]["opening"]
    rows = []
    for pid in st.user_payments.get(uid, ()):
        p = st.payments[pid]
        rev = st.selected_revision(p, known)
        if rev is None:
            continue
        delta = st.signed(p, uid, rev["amount"])
        eff = rev["effective_ts"]
        if start is not None and eff < start:
            opening += delta
        elif eff < end:
            rows.append((eff, pid, p, rev, delta))
    rows.sort(key=lambda r: (r[0], r[1]))
    running = opening
    entries = []
    for _, _, p, rev, delta in rows:
        running += delta
        entries.append({"payment": st.payment_view(p, amount=rev["amount"]),
                        "delta": delta, "balance_after": running,
                        "revision": rev["revision"], "effective_at": rev["effective_at"],
                        "recorded_at": rev["recorded_at"]})
    return {"opening_balance": opening, "closing_balance": running, "entries": entries}


def h_statement(ctx: Ctx):
    with LOCK:
        st = STATE
        user = ctx.user(st)
        token = ctx.query.get("snapshot", [None])[0]
        if token is not None:
            if any(k in ctx.query for k in ("from", "to", "known_at")):
                raise invalid("only limit and offset may accompany a snapshot")
            limit, offset = paging(ctx.query)
            snap = st.snapshots.get(token)
            if snap is None or snap["user_id"] != user["id"]:
                raise not_found("unknown statement snapshot")
        else:
            from_raw, start = query_instant(ctx, "from")
            to_raw, end = query_instant(ctx, "to")
            known_raw, known = query_instant(ctx, "known_at")
            limit, offset = paging(ctx.query)
            now = st.now()
            if end is None:
                end = now
                to_raw = fmt_us(now)
            if known is None:
                known = now
            if start is not None and start > end:
                raise invalid("from must not be after to")
            snap = build_statement(st, user["id"], start, end, known)
            snap.update({"user_id": user["id"], "from": from_raw, "to": to_raw,
                         "known_at": known_raw})
            token = "ss_" + secrets.token_urlsafe(18)
            st.snapshots[token] = snap
        chunk, more = page(snap["entries"], limit, offset)
        body = {"opening_balance": snap["opening_balance"], "entries": chunk,
                "closing_balance": snap["closing_balance"], "has_more": more,
                "snapshot": token, "from": snap["from"], "to": snap["to"]}
        if snap.get("known_at") is not None:
            body["known_at"] = snap["known_at"]
        return 200, body


def parse_correction(body: dict, now: int):
    """Validate the ordinary correction fields. Returns (expected, amount, eff_raw, eff, reason)."""
    for f in ("expected_revision", "amount", "effective_at", "reason"):
        if f not in body:
            raise invalid(f"{f} is required")
    er = body["expected_revision"]
    if not is_int_value(er) or int(er) < 1:
        raise invalid("expected_revision must be a positive integer")
    amount = to_amount(body["amount"], allow_zero=True)
    reason = body["reason"]
    if not isinstance(reason, str) or not 1 <= len(reason) <= MAX_NOTE:
        raise invalid("reason must be 1 to 200 characters")
    eff_raw = body["effective_at"]
    try:
        eff = parse_us(eff_raw)
    except Exception:
        raise invalid("effective_at must be an RFC 3339 instant with an offset") from None
    if eff > now:
        raise invalid("effective_at must not be in the future")
    return int(er), amount, eff_raw, eff, reason


def check_correctable(st: State, p: dict, expected: int, amount: int) -> dict:
    """Rules shared by single and batch corrections; returns the current revision."""
    if st.immutable(p):
        raise ApiError(422, "linked_payment_immutable",
                       "captures and refunds cannot be corrected")
    current = p["revisions"][-1]
    if expected != current["revision"]:
        raise ApiError(409, "stale_revision", "expected_revision is not the latest")
    if amount < st.refunded(p):
        raise ApiError(422, "refund_exceeds_payment",
                       "a payment cannot be corrected below its refunded amount")
    return current


def apply_corrections(st: State, plan: list, recorded: int, batch_id=None) -> list:
    """Check affordability (current, then historical) for the combined effect of all
    planned revisions, then append them atomically. plan: [(payment, amount, eff_raw,
    eff, reason)]. Raises without changing anything on failure."""
    net: dict[str, int] = {}
    revs: dict[str, dict] = {}
    for p, amount, eff_raw, eff, reason in plan:
        current = p["revisions"][-1]
        diff = amount - current["amount"]
        net[p["from_user_id"]] = net.get(p["from_user_id"], 0) - diff
        net[p["to_user_id"]] = net.get(p["to_user_id"], 0) + diff
        revs[p["id"]] = {"revision": current["revision"] + 1, "amount": amount,
                         "effective_at": eff_raw, "effective_ts": eff,
                         "recorded_at": fmt_us(recorded), "recorded_ts": recorded,
                         "reason": reason, "correction_batch_id": batch_id}
    for uid, d in net.items():
        if d < 0 and st.available(st.users[uid]) + d < 0:
            raise ApiError(409, "insufficient_funds",
                           "a wallet cannot currently fund the correction")
    for uid in net:
        if st.correction_overdrafts(uid, revs):
            raise ApiError(409, "historical_overdraft",
                           "the correction would overdraw a wallet in the past")
    out = []
    for p, *_ in plan:
        rev = revs[p["id"]]
        p["revisions"].append(rev)
        out.append(st.revision_view(p, rev))
    for uid, d in net.items():
        st.users[uid]["balance"] += d
    st.last_ts = max(st.last_ts, recorded)
    return out


def correction_handler(payment_id: str):
    def run(st: State, user: dict, body: dict):
        now = st.now()
        expected, amount, eff_raw, eff, reason = parse_correction(body, now)
        p = st.payments.get(payment_id)
        if p is None:
            raise not_found("unknown payment")
        if p["from_user_id"] != user["id"]:
            raise forbidden("only the original sender may correct a payment")
        if p.get("settlement_id") is not None:
            raise ApiError(422, "linked_payment_immutable",
                           "settlement members are corrected through correction batches")
        current = check_correctable(st, p, expected, amount)
        recorded = max(now, current["recorded_ts"] + 1)
        return 201, apply_corrections(st, [(p, amount, eff_raw, eff, reason)], recorded)[0]
    return run


def do_correction_batch(st: State, user: dict, body: dict):
    now = st.now()
    items = body.get("corrections")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_TRANSFERS:
        raise invalid("corrections must contain 1 to 32 objects")
    if not all(isinstance(it, dict) for it in items):
        raise invalid("each correction must be an object")
    ids = [it.get("payment_id") for it in items]
    if not all(isinstance(x, str) for x in ids):
        raise invalid("each correction needs a payment_id string")
    if len(set(ids)) != len(ids):
        raise invalid("payment_ids must be distinct")
    plan = []
    for idx, it in enumerate(items):
        expected, amount, eff_raw, eff, reason = parse_correction(it, now)
        p = st.payments.get(it["payment_id"])
        if p is None:
            raise not_found(f"correction {idx}: unknown payment")
        check_correctable(st, p, expected, amount)
        plan.append((p, amount, eff_raw, eff, reason))
    included = set(ids)
    settlements = {}
    for p, amount, eff_raw, eff, reason in plan:
        sid = p.get("settlement_id")
        if sid is not None:
            settlements.setdefault(sid, []).append(eff)
    for sid in settlements:
        members = [x for x in st.payments.values() if x.get("settlement_id") == sid]
        if any(m["id"] not in included for m in members):
            raise ApiError(422, "incomplete_settlement",
                           "every member of the settlement must be corrected together")
    for sid, effs in settlements.items():
        if len(set(effs)) != 1:
            raise invalid("members of one settlement need identical effective instants")
    recorded = max([now] + [p["revisions"][-1]["recorded_ts"] + 1 for p, *_ in plan])
    batch_id = new_id("cb")
    revisions = apply_corrections(st, plan, recorded, batch_id)
    st.batches[batch_id] = {"id": batch_id, "operator_id": user["id"],
                            "payment_ids": ids, "recorded_at": fmt_us(recorded)}
    return 201, {"correction_batch_id": batch_id, "recorded_at": fmt_us(recorded),
                 "revisions": revisions}


def refund_handler(payment_id: str):
    def run(st: State, user: dict, body: dict):
        if "amount" not in body:
            raise invalid("amount is required")
        amount = to_amount(body["amount"])
        p = st.payments.get(payment_id)
        if p is None:
            raise not_found("unknown payment")
        if p["to_user_id"] != user["id"]:
            raise forbidden("only the receiver may refund a payment")
        if p.get("refund_of") is not None:
            raise ApiError(422, "invalid_refund_target", "a refund cannot be refunded")
        if st.refunded(p) + amount > p["revisions"][-1]["amount"]:
            raise ApiError(422, "refund_exceeds_payment",
                           "refunds would exceed the payment's corrected amount")
        receiver, sender = st.users[p["to_user_id"]], st.users[p["from_user_id"]]
        if st.available(receiver) < amount:
            raise ApiError(409, "insufficient_funds", "available balance is below amount")
        ts = st.now()
        receiver["balance"] -= amount
        sender["balance"] += amount
        r = st.new_payment_record(receiver, sender, amount, p["note"], p["visibility"], ts,
                                  refund_of=p["id"])
        p["refund_ids"].append(r["id"])
        return 201, st.payment_view(r)
    return run


def h_revisions(ctx: Ctx, payment_id: str):
    with LOCK:
        st = STATE
        user = ctx.user(st)
        p = st.payments.get(payment_id)
        if p is None or user["id"] not in (p["from_user_id"], p["to_user_id"]):
            raise not_found("unknown payment")
        return 200, {"revisions": [st.revision_view(p, r) for r in p["revisions"]]}


def do_payment(st: State, user: dict, body: dict):
    to_handle = req_string(body, "to_handle")
    if "amount" not in body:
        raise invalid("amount is required")
    amount = to_amount(body["amount"])
    note = opt_note(body)
    vis = opt_visibility(body)
    to = st.user_by_handle(to_handle)
    if to is None:
        raise not_found("no user has that handle")
    if to["id"] == user["id"]:
        raise ApiError(422, "self_payment", "cannot pay yourself")
    p = st.make_payment(user, to, amount, note, vis)
    return 201, st.payment_view(p)


def do_create_request(st: State, user: dict, body: dict):
    payer_handle = req_string(body, "payer_handle")
    if "amount" not in body:
        raise invalid("amount is required")
    amount = to_amount(body["amount"])
    note = opt_note(body)
    payer = st.user_by_handle(payer_handle)
    if payer is None:
        raise not_found("no user has that handle")
    if payer["id"] == user["id"]:
        raise ApiError(422, "self_request", "cannot request from yourself")
    r = st.make_request(user, payer, amount, note)
    return 201, st.request_view(r)


def pay_request_handler(request_id: str):
    def run(st: State, user: dict, body: dict):
        vis = opt_visibility(body)
        r = st.requests.get(request_id)
        if r is None:
            raise not_found("unknown request")
        if r["payer_id"] != user["id"]:
            raise forbidden("only the payer may pay this request")
        if r["status"] != "pending":
            raise ApiError(409, "request_not_pending", "request is not pending")
        requester = st.users[r["requester_id"]]
        p = st.make_payment(user, requester, r["amount"], r["note"], vis,
                            request_id=r["id"])
        r["status"] = "paid"
        r["payment_id"] = p["id"]
        return 201, st.payment_view(p)
    return run


def h_request_transition(ctx: Ctx, request_id: str, action: str):
    with LOCK:
        st = STATE
        user = ctx.user(st)
        r = st.requests.get(request_id)
        if r is None:
            raise not_found("unknown request")
        if action == "decline":
            if r["payer_id"] != user["id"]:
                raise forbidden("only the payer may decline this request")
            target = "declined"
        else:
            if r["requester_id"] != user["id"]:
                raise forbidden("only the requester may cancel this request")
            target = "cancelled"
        if r["status"] == target:
            return 200, st.request_view(r)
        if r["status"] != "pending":
            raise ApiError(409, "request_not_pending", "request is not pending")
        r["status"] = target
        return 200, st.request_view(r)


def h_list_requests(ctx: Ctx):
    with LOCK:
        st = STATE
        user = ctx.user(st)
        direction = ctx.query.get("direction", [None])[0]
        status = ctx.query.get("status", [None])[0]
        if direction is not None and direction not in ("incoming", "outgoing"):
            raise invalid("direction must be incoming or outgoing")
        if status is not None and status not in STATUSES:
            raise invalid("unknown status")
        limit, offset = paging(ctx.query)
        uid = user["id"]
        items = []
        for r in st.requests.values():
            if direction == "incoming":
                mine = r["payer_id"] == uid
            elif direction == "outgoing":
                mine = r["requester_id"] == uid
            else:
                mine = r["payer_id"] == uid or r["requester_id"] == uid
            if mine and (status is None or r["status"] == status):
                items.append(r)
        items.sort(key=lambda r: (r["ts"], r["seq"]), reverse=True)
        chunk, more = page(items, limit, offset)
        return 200, {"requests": [st.request_view(r) for r in chunk], "has_more": more}


def equal_split(amount: int, n: int) -> list[int]:
    base, rem = divmod(amount, n)
    return [base + (1 if i < rem else 0) for i in range(n)]


def do_split(st: State, user: dict, body: dict):
    if "amount" not in body:
        raise invalid("amount is required")
    handles = body.get("participant_handles")
    if handles is None:
        raise invalid("participant_handles is required")
    if not isinstance(handles, list):
        raise malformed("participant_handles must be an array")
    if not all(isinstance(h, str) for h in handles):
        raise malformed("participant_handles must contain strings")
    amount = to_amount(body["amount"])
    if not handles:
        raise invalid("participant_handles must not be empty")
    if len(set(handles)) != len(handles):
        raise invalid("participant_handles contains a duplicate")
    note = opt_note(body)
    people = []
    for h in handles:
        u = st.user_by_handle(h)
        if u is None:
            raise not_found(f"unknown handle {h}")
        people.append(u)
    shares = equal_split(amount, len(people))
    ts = st.now()
    reqs = [st.make_request(user, u, share, note, ts=ts)
            for u, share in zip(people, shares) if u["id"] != user["id"]]
    sid = new_id("sp")
    response = {
        "split_id": sid,
        "amount": amount,
        "currency": st.currency,
        "note": note,
        "shares": [{"handle": u["handle"], "amount": s} for u, s in zip(people, shares)],
        "requests": [st.request_view(r) for r in reqs],
        "created_at": fmt_us(ts),
    }
    st.splits[sid] = {"id": sid, "requester_id": user["id"], "amount": amount,
                      "note": note, "request_ids": [r["id"] for r in reqs],
                      "created_at": response["created_at"]}
    return 201, response


def do_settlement(st: State, user: dict, body: dict):
    transfers = body.get("transfers")
    if not isinstance(transfers, list) or not 1 <= len(transfers) <= MAX_TRANSFERS:
        raise invalid("transfers must contain 1 to 32 objects")
    plan = []
    for idx, t in enumerate(transfers):
        if not isinstance(t, dict):
            raise invalid(f"transfer {idx} must be an object")
        fh, th = t.get("from_handle"), t.get("to_handle")
        if not isinstance(fh, str) or not isinstance(th, str):
            raise invalid(f"transfer {idx} needs from_handle and to_handle strings")
        if "amount" not in t:
            raise invalid(f"transfer {idx} amount is required")
        amount = to_amount(t["amount"])
        note = opt_note(t)
        vis = opt_visibility(t)
        frm, to = st.user_by_handle(fh), st.user_by_handle(th)
        if frm is None or to is None:
            raise not_found(f"transfer {idx} names an unknown handle")
        if frm["id"] == to["id"]:
            raise ApiError(422, "self_payment", f"transfer {idx} is a self-transfer")
        plan.append((frm, to, amount, note, vis))
    net: dict[str, int] = {}
    for frm, to, amount, _, _ in plan:
        net[frm["id"]] = net.get(frm["id"], 0) - amount
        net[to["id"]] = net.get(to["id"], 0) + amount
    # Held funds cannot fund a net debit: check against available, not total.
    if any(st.available(st.users[uid]) + d < 0 for uid, d in net.items()):
        raise ApiError(409, "insufficient_funds", "settlement is not affordable")
    sid = new_id("st")
    ts = st.now()
    # Apply the net movements in one step; record each transfer as a payment.
    for uid, d in net.items():
        st.users[uid]["balance"] += d
    payments = []
    for frm, to, amount, note, vis in plan:
        payments.append(st.new_payment_record(frm, to, amount, note, vis, ts,
                                              settlement_id=sid))
    st.settlements[sid] = {"id": sid, "operator_id": user["id"],
                           "payment_ids": [p["id"] for p in payments],
                           "committed_at": fmt_us(ts)}
    return 201, {"settlement_id": sid, "committed_at": fmt_us(ts),
                 "payments": [st.payment_view(p) for p in payments]}


def do_authorize(st: State, user: dict, body: dict):
    to_handle = req_string(body, "to_handle")
    if "amount" not in body:
        raise invalid("amount is required")
    amount = to_amount(body["amount"])
    note = opt_note(body)
    vis = opt_visibility(body)
    to = st.user_by_handle(to_handle)
    if to is None:
        raise not_found("no user has that handle")
    if to["id"] == user["id"]:
        raise ApiError(422, "self_payment", "cannot authorize a payment to yourself")
    if st.available(user) < amount:
        raise ApiError(409, "insufficient_funds", "available balance is below amount")
    ts = st.now()
    exp = ts + st.auth_ttl * 1_000_000
    a = {"id": new_id("a"), "from_user_id": user["id"], "to_user_id": to["id"],
         "amount": amount, "captured_amount": 0, "note": note, "visibility": vis,
         "status": "open", "expires_at": fmt_us(exp), "expires_ts": exp,
         "payment_ids": [], "payment_id": None, "created_at": fmt_us(ts), "ts": ts,
         "seq": st.next_seq(), "initial_held": amount, "closed_ts": None, "close_kind": None}
    st.index_auth(a)
    return 201, st.auth_view(a)


def capture_handler(auth_id: str):
    def run(st: State, user: dict, body: dict):
        amount = None
        if "amount" in body:
            v = body["amount"]
            if not is_int_value(v) or int(v) < 1:
                raise invalid("amount must be a positive integer")
            amount = int(v)
        final = True
        if "final" in body:
            if not isinstance(body["final"], bool):
                raise malformed("final must be a boolean")
            final = body["final"]
        a = st.auths.get(auth_id)
        if a is None:
            raise not_found("unknown authorization")
        if a["to_user_id"] != user["id"]:
            raise forbidden("only the receiver may capture")
        if a["status"] == "expired" or (a["status"] == "open"
                                        and a["expires_ts"] <= st.now()):
            raise ApiError(409, "authorization_expired", "authorization has expired")
        if a["status"] != "open":
            raise ApiError(409, "authorization_not_open", "authorization is not open")
        remaining = a["amount"] - a["captured_amount"]
        if amount is None:
            amount = remaining
        if amount > remaining:
            raise ApiError(422, "capture_exceeds_authorization",
                           "amount exceeds the uncaptured remainder")
        payer, receiver = st.users[a["from_user_id"]], st.users[a["to_user_id"]]
        p = st.make_payment(payer, receiver, amount, a["note"], a["visibility"],
                            authorization_id=a["id"])
        a["captured_amount"] += amount
        a["payment_ids"].append(p["id"])
        a["payment_id"] = p["id"]
        if final or a["captured_amount"] >= a["amount"]:
            a["status"] = "captured"
            a["closed_ts"], a["close_kind"] = p["ts"], "captured"
            st.open_auths.discard(a["id"])
        return 201, st.payment_view(p)
    return run


def h_void(ctx: Ctx, auth_id: str):
    with LOCK:
        st = STATE
        user = ctx.user(st)
        a = st.auths.get(auth_id)
        if a is None:
            raise not_found("unknown authorization")
        if a["from_user_id"] != user["id"]:
            raise forbidden("only the payer may void")
        if a["status"] == "voided":
            return 200, st.auth_view(a)
        if a["status"] != "open":
            raise ApiError(409, "authorization_not_open", "authorization is not open")
        a["status"] = "voided"
        a["closed_ts"], a["close_kind"] = st.now(), "voided"
        st.open_auths.discard(a["id"])
        return 200, st.auth_view(a)


def h_list_auths(ctx: Ctx):
    with LOCK:
        st = STATE
        user = ctx.user(st)
        direction = ctx.query.get("direction", [None])[0]
        status = ctx.query.get("status", [None])[0]
        if direction is not None and direction not in ("incoming", "outgoing"):
            raise invalid("direction must be incoming or outgoing")
        if status is not None and status not in AUTH_STATUSES:
            raise invalid("unknown status")
        limit, offset = paging(ctx.query)
        uid = user["id"]
        items = []
        for a in st.auths.values():
            if direction == "incoming":
                mine = a["to_user_id"] == uid
            elif direction == "outgoing":
                mine = a["from_user_id"] == uid
            else:
                mine = a["to_user_id"] == uid or a["from_user_id"] == uid
            if mine and (status is None or a["status"] == status):
                items.append(a)
        items.sort(key=lambda a: (a["ts"], a["seq"]), reverse=True)
        chunk, more = page(items, limit, offset)
        return 200, {"authorizations": [st.auth_view(a) for a in chunk],
                     "has_more": more}


def h_activity(ctx: Ctx):
    with LOCK:
        st = STATE
        user = ctx.user(st)
        limit, offset = paging(ctx.query)
        uid = user["id"]
        items = [p for p in st.payments.values()
                 if p["visibility"] == "public" or p["from_user_id"] == uid
                 or p["to_user_id"] == uid]
        items.sort(key=lambda p: (p["ts"], p["seq"]), reverse=True)
        chunk, more = page(items, limit, offset)
        return 200, {"payments": [st.payment_view(p) for p in chunk], "has_more": more}


# -- test control -----------------------------------------------------------

def h_reset(ctx: Ctx):
    global STATE
    fx = parse_json(ctx.raw) if ctx.raw.strip() else None
    if fx is None:
        raise malformed("fixture body is required")
    new_state = state_from_fixture(fx)
    with LOCK:
        STATE = new_state
    with _VERIFIED_LOCK:
        _VERIFIED.clear()
    return 204, None


def h_export(ctx: Ctx):
    with LOCK:
        STATE.expire_holds()
        snapshot = json.loads(json.dumps(STATE.to_dict()))
    return 200, {"track": TRACK, "format_version": FORMAT_VERSION, "state": snapshot}


def h_import(ctx: Ctx):
    global STATE
    doc = ctx.json_body()
    if doc.get("track") != TRACK:
        raise invalid("track must be pocketful")
    fv = doc.get("format_version")
    if isinstance(fv, bool) or fv != FORMAT_VERSION:
        raise invalid("unsupported format_version")
    if not isinstance(doc.get("state"), dict):
        raise invalid("state must be an object")
    try:
        new_state = State.from_dict(copy.deepcopy(doc["state"]))
    except ApiError:
        raise
    except Exception:
        raise invalid("invalid state") from None
    with LOCK:
        STATE = new_state
    return 204, None


# -- routing ----------------------------------------------------------------

PAYMENT_SUB_RE = re.compile(r"/payments/([^/]+)/(corrections|revisions|refunds)")
AUTH_ACTION_RE = re.compile(r"/authorizations/([^/]+)/(capture|void)")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
UI_ROUTES = ("/", "/split", "/signup", "/login")
NEGOTIATED_ROUTES = ("/requests", "/authorizations")
STATIC_TYPES = {".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".html": "text/html; charset=utf-8"}


class Raw:
    """A non-JSON response body (HTML and static assets)."""
    def __init__(self, data: bytes, content_type: str, cache: str = "no-cache"):
        self.data = data
        self.content_type = content_type
        self.cache = cache


def _load_static() -> dict:
    files = {}
    for name in ("index.html", "app.js", "app.css"):
        with open(os.path.join(STATIC_DIR, name), "rb") as f:
            files[name] = f.read()
    return files


STATIC = _load_static()


def wants_html(ctx) -> bool:
    return "text/html" in (ctx.headers.get("Accept") or "").lower()


def ui_page():
    return 200, Raw(STATIC["index.html"], STATIC_TYPES[".html"])


REQ_ACTION_RE = re.compile(r"/requests/([^/]+)/(pay|decline|cancel)")


def route(ctx: Ctx):
    m, path = ctx.method, ctx.path
    if m in ("GET", "HEAD"):
        if path in UI_ROUTES or (path in NEGOTIATED_ROUTES and wants_html(ctx)):
            return ui_page()
        if path.startswith("/static/"):
            name = path[len("/static/"):]
            if name in STATIC and name != "index.html":
                return 200, Raw(STATIC[name], STATIC_TYPES[os.path.splitext(name)[1]])
            raise not_found("no such asset")
    if path == "/health":
        if m == "GET":
            return 200, {"status": "ok"}
        raise method_not_allowed()
    if path == "/_test/reset":
        return h_reset(ctx) if m == "POST" else method_not_allowed_raise()
    if path == "/_test/export":
        return h_export(ctx) if m == "GET" else method_not_allowed_raise()
    if path == "/_test/import":
        return h_import(ctx) if m == "POST" else method_not_allowed_raise()
    if path == "/auth/signup":
        return h_signup(ctx) if m == "POST" else method_not_allowed_raise()
    if path == "/auth/login":
        return h_login(ctx) if m == "POST" else method_not_allowed_raise()
    if path == "/me":
        return h_me(ctx) if m == "GET" else method_not_allowed_raise()
    if path == "/payments":
        return idempotent(ctx, do_payment) if m == "POST" else method_not_allowed_raise()
    if path == "/requests":
        if m == "POST":
            return idempotent(ctx, do_create_request)
        if m == "GET":
            return h_list_requests(ctx)
        raise method_not_allowed()
    if path == "/splits":
        return idempotent(ctx, do_split) if m == "POST" else method_not_allowed_raise()
    if path == "/settlements":
        return idempotent(ctx, do_settlement) if m == "POST" else method_not_allowed_raise()
    if path == "/activity":
        return h_activity(ctx) if m == "GET" else method_not_allowed_raise()
    if path == "/authorizations":
        if m == "POST":
            return idempotent(ctx, do_authorize)
        if m == "GET":
            return h_list_auths(ctx)
        raise method_not_allowed()
    if path == "/correction-batches":
        if m != "POST":
            raise method_not_allowed()
        return idempotent(ctx, do_correction_batch)
    if path == "/statement":
        return h_statement(ctx) if m == "GET" else method_not_allowed_raise()
    match = PAYMENT_SUB_RE.fullmatch(path)
    if match:
        pid, sub = match.group(1), match.group(2)
        if sub == "corrections":
            if m != "POST":
                raise method_not_allowed()
            return idempotent(ctx, correction_handler(pid))
        if sub == "refunds":
            if m != "POST":
                raise method_not_allowed()
            return idempotent(ctx, refund_handler(pid))
        if m != "GET":
            raise method_not_allowed()
        return h_revisions(ctx, pid)
    match = AUTH_ACTION_RE.fullmatch(path)
    if match:
        if m != "POST":
            raise method_not_allowed()
        aid, action = match.group(1), match.group(2)
        if action == "capture":
            return idempotent(ctx, capture_handler(aid))
        return h_void(ctx, aid)
    match = REQ_ACTION_RE.fullmatch(path)
    if match:
        if m != "POST":
            raise method_not_allowed()
        rid, action = match.group(1), match.group(2)
        if action == "pay":
            return idempotent(ctx, pay_request_handler(rid))
        return h_request_transition(ctx, rid, action)
    raise not_found("no such route")


def method_not_allowed():
    return ApiError(405, "method_not_allowed", "method not allowed")


def method_not_allowed_raise():
    raise method_not_allowed()


def encode(payload) -> bytes:
    try:
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(payload, ensure_ascii=True).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "pocketful/1"
    sys_version = ""

    def log_message(self, fmt, *args):  # quiet access log
        pass

    def _read_body(self) -> bytes:
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            chunks, total = [], 0
            while True:
                line = self.rfile.readline(65537)
                size = int(line.split(b";", 1)[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline(65537) not in (b"\r\n", b"\n", b""):
                        pass
                    break
                total += size
                if total > MAX_BODY:
                    raise ApiError(413, "payload_too_large", "body too large")
                chunks.append(self.rfile.read(size))
                self.rfile.readline(65537)
            return b"".join(chunks)
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            n = int(length)
        except ValueError:
            self.close_connection = True
            raise malformed("invalid Content-Length") from None
        if n < 0:
            self.close_connection = True
            raise malformed("invalid Content-Length")
        if n > MAX_BODY:
            self.close_connection = True
            raise ApiError(413, "payload_too_large", "body too large")
        return self.rfile.read(n)

    def _handle(self, method: str):
        status, payload = 500, None
        try:
            try:
                raw = self._read_body()
            except ApiError:
                raise
            except Exception:
                self.close_connection = True
                raise malformed("could not read body") from None
            parts = urlsplit(self.path)
            path = parts.path
            if len(path) > 1 and path.endswith("/"):
                path = path.rstrip("/") or "/"
            # A literal "+" in a query value is kept (RFC 3339 offsets), not read as a space.
            query = parse_qs(parts.query.replace("+", "%2B"), keep_blank_values=True)
            ctx = Ctx(self, method, path, query, self.headers, raw)
            status, payload = route(ctx)
        except ApiError as e:
            status, payload = e.status, {"error": {"code": e.code, "message": e.message}}
        except Exception:
            traceback.print_exc(file=sys.stderr)
            status = 500
            payload = {"error": {"code": "internal_error", "message": "internal error"}}
        self._send(status, payload)

    def _send(self, status: int, payload):
        try:
            if status == 204 or payload is None:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if isinstance(payload, Raw):
                self.send_response(status)
                self.send_header("Content-Type", payload.content_type)
                self.send_header("Content-Length", str(len(payload.data)))
                self.send_header("Cache-Control", payload.cache)
                self.send_header("Vary", "Accept")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload.data)
                return
            data = encode(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Vary", "Accept")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def do_GET(self):
        self._handle("GET")

    def do_HEAD(self):
        self._handle("HEAD")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_DELETE(self):
        self._handle("DELETE")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256


def main():
    port = int(os.environ.get("PORT", "8080") or "8080")
    server = Server(("0.0.0.0", port), Handler)
    print(f"pocketful listening on 0.0.0.0:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
