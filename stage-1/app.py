"""Pocketful stage 1: payments, requests, splits, activity feed and settlements.

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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

TRACK = "pocketful"
FORMAT_VERSION = 1

MAX_AMOUNT = 1_000_000_000
MAX_NOTE = 200
MAX_KEY = 255
MAX_BODY = 8 * 1024 * 1024
MAX_TRANSFERS = 32
HANDLE_RE = re.compile(r"^[a-z0-9_]{1,20}$")
DIGITS_RE = re.compile(r"^[0-9]+$")
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


def fmt_ts(epoch: float) -> str:
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}+00:00"


def parse_ts(value) -> float:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError("timestamp needs an offset")
    return dt.timestamp()


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
    def __init__(self, currency: str, minor_units: int):
        self.currency = currency
        self.minor_units = minor_units
        self.users: dict[str, dict] = {}       # id -> user
        self.by_email: dict[str, str] = {}     # lowercased email -> id
        self.by_handle: dict[str, str] = {}    # handle -> id
        self.tokens: dict[str, str] = {}       # token -> user id
        self.payments: dict[str, dict] = {}    # id -> payment (insertion order = seq)
        self.requests: dict[str, dict] = {}
        self.splits: dict[str, dict] = {}
        self.settlements: dict[str, dict] = {}
        self.operators: set[str] = set()
        self.idem: dict[str, dict] = {}        # scope key -> record
        self.seq = 0
        self.last_ts = 0.0

    # -- clock and sequencing ------------------------------------------------
    def now(self) -> float:
        ts = max(time.time(), self.last_ts)
        self.last_ts = ts
        return ts

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    # -- users ---------------------------------------------------------------
    def add_user(self, uid, email, pw_hash, display_name, handle, balance):
        self.users[uid] = {"id": uid, "email": email, "password_hash": pw_hash,
                           "display_name": display_name, "handle": handle,
                           "balance": balance}
        self.by_email[email.lower()] = uid
        self.by_handle[handle] = uid

    def user_by_handle(self, handle):
        uid = self.by_handle.get(handle) if isinstance(handle, str) else None
        return self.users.get(uid) if uid else None

    # -- views ---------------------------------------------------------------
    def payment_view(self, p: dict) -> dict:
        frm, to = self.users[p["from_user_id"]], self.users[p["to_user_id"]]
        return {
            "payment_id": p["id"],
            "from_user_id": frm["id"],
            "from_handle": frm["handle"],
            "to_user_id": to["id"],
            "to_handle": to["handle"],
            "amount": p["amount"],
            "currency": self.currency,
            "note": p["note"],
            "visibility": p["visibility"],
            "request_id": p["request_id"],
            "settlement_id": p["settlement_id"],
            "created_at": p["created_at"],
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

    # -- money ---------------------------------------------------------------
    def make_payment(self, frm: dict, to: dict, amount: int, note: str, visibility: str,
                     *, request_id=None, settlement_id=None, ts=None) -> dict:
        if frm["balance"] < amount:
            raise ApiError(409, "insufficient_funds", "balance is below amount")
        ts = self.now() if ts is None else ts
        frm["balance"] -= amount
        to["balance"] += amount
        p = {"id": new_id("p"), "from_user_id": frm["id"], "to_user_id": to["id"],
             "amount": amount, "note": note, "visibility": visibility,
             "request_id": request_id, "settlement_id": settlement_id,
             "created_at": fmt_ts(ts), "ts": ts, "seq": self.next_seq()}
        self.payments[p["id"]] = p
        return p

    def make_request(self, requester: dict, payer: dict, amount: int, note: str,
                     ts=None) -> dict:
        ts = self.now() if ts is None else ts
        r = {"id": new_id("rq"), "requester_id": requester["id"], "payer_id": payer["id"],
             "amount": amount, "note": note, "status": "pending", "payment_id": None,
             "created_at": fmt_ts(ts), "ts": ts, "seq": self.next_seq()}
        self.requests[r["id"]] = r
        return r

    # -- serialisation -------------------------------------------------------
    def to_dict(self) -> dict:
        return {
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
            "seq": self.seq,
            "last_ts": self.last_ts,
        }

    @classmethod
    def from_dict(cls, d) -> "State":
        """Rebuild exported state, validating it strictly. Raises on anything invalid."""
        def need(cond, msg="invalid state"):
            if not cond:
                raise ValueError(msg)

        def s(v):
            need(isinstance(v, str))
            return v

        def i(v):
            need(isinstance(v, int) and not isinstance(v, bool))
            return v

        def num(v):
            need(isinstance(v, (int, float)) and not isinstance(v, bool)
                 and math.isfinite(v))
            return float(v)

        def opt_s(v):
            need(v is None or isinstance(v, str))
            return v

        need(isinstance(d, dict))
        currency = s(d["currency"])
        mu = i(d["minor_units"])
        need(mu in (0, 2, 3))
        st = cls(currency, mu)
        for u in d["users"]:
            need(isinstance(u, dict))
            uid, email, handle = s(u["id"]), s(u["email"]), s(u["handle"])
            need(0 < len(uid) <= 64 and HANDLE_RE.match(handle) is not None)
            need(uid not in st.users and handle not in st.by_handle
                 and email.lower() not in st.by_email)
            check_hash_format(u["password_hash"])
            bal = i(u["balance"])
            need(0 <= bal <= 2 ** 53)
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
            parse_ts(p["created_at"])
            st.payments[pid] = {
                "id": pid, "from_user_id": p["from_user_id"], "to_user_id": p["to_user_id"],
                "amount": p["amount"], "note": p["note"], "visibility": p["visibility"],
                "request_id": p["request_id"], "settlement_id": p["settlement_id"],
                "created_at": p["created_at"], "ts": num(p["ts"]), "seq": i(p["seq"])}
        for r in d["requests"]:
            need(isinstance(r, dict))
            rid = s(r["id"])
            need(0 < len(rid) <= 64 and rid not in st.requests)
            need(s(r["requester_id"]) in st.users and s(r["payer_id"]) in st.users)
            need(i(r["amount"]) >= 0)
            s(r["note"])
            need(r["status"] in STATUSES)
            opt_s(r["payment_id"])
            parse_ts(r["created_at"])
            st.requests[rid] = {
                "id": rid, "requester_id": r["requester_id"], "payer_id": r["payer_id"],
                "amount": r["amount"], "note": r["note"], "status": r["status"],
                "payment_id": r["payment_id"], "created_at": r["created_at"],
                "ts": num(r["ts"]), "seq": i(r["seq"])}
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
        st.seq = max(i(d["seq"]), max((x["seq"] for x in st.payments.values()), default=0),
                     max((x["seq"] for x in st.requests.values()), default=0))
        st.last_ts = num(d["last_ts"])
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
    base = time.time()
    st.last_ts = base

    for u in users:
        need(isinstance(u, dict), "user must be an object")
        for f in ("id", "email", "password", "display_name", "handle"):
            need(isinstance(u.get(f), str), f"user {f} must be a string")
        need(0 < len(u["id"]) <= 64, "user id must be 1..64 characters")
        need(HANDLE_RE.match(u["handle"]) is not None, "invalid handle")
        need(is_int_value(u.get("balance")), "balance must be an integer")
        bal = int(u["balance"])
        need(bal >= 0, "balance must not be negative")
        need(bal <= 2 ** 53, "balance out of range")
        need(u["id"] not in st.users, "duplicate user id")
        need(u["handle"] not in st.by_handle, "duplicate handle")
        need(u["email"].lower() not in st.by_email, "duplicate email")
        st.add_user(u["id"], u["email"], "", u["display_name"], u["handle"], bal)

    # Hash each distinct password once, in parallel (scrypt releases the GIL).
    distinct = list(dict.fromkeys(u["password"] for u in users))
    with ThreadPoolExecutor(max_workers=4) as pool:
        hashes = dict(zip(distinct, pool.map(
            lambda pw: hash_password(pw, SEED_SCRYPT_N), distinct)))
    for u in users:
        st.users[u["id"]]["password_hash"] = hashes[u["password"]]

    def seeded_ts(item):
        if "created_at" in item and item["created_at"] is not None:
            try:
                return parse_ts(item["created_at"])
            except Exception:
                raise invalid("invalid created_at") from None
        return None

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
        ts = seeded_ts(p)
        if ts is None:
            ts = base
        st.payments[pid] = {
            "id": pid, "from_user_id": p["from_user_id"], "to_user_id": p["to_user_id"],
            "amount": int(p["amount"]), "note": note, "visibility": vis,
            "request_id": rid, "settlement_id": sid, "created_at": fmt_ts(ts)
            if "created_at" not in p or p["created_at"] is None else p["created_at"],
            "ts": ts, "seq": st.next_seq()}

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
        if ts is None:
            ts = base
        st.requests[rid] = {
            "id": rid, "requester_id": r["requester_id"], "payer_id": r["payer_id"],
            "amount": int(r["amount"]), "note": note, "status": status,
            "payment_id": pay_id, "created_at": fmt_ts(ts)
            if "created_at" not in r or r["created_at"] is None else r["created_at"],
            "ts": ts, "seq": st.next_seq()}

    for op in operators:
        need(isinstance(op, str), "settlement_operator_ids must be strings")
        st.operators.add(op)

    st.last_ts = max([base] + [x["ts"] for x in st.payments.values()]
                     + [x["ts"] for x in st.requests.values()])
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
        if not DIGITS_RE.match(text):
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
        if ctx.path == "/settlements" and user["id"] not in st.operators:
            raise forbidden("settlement operator permission required")
        key = ctx.idem_key()
        body = ctx.json_body(empty_as_object=ctx.path.endswith("/pay"))
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

def h_me(ctx: Ctx):
    with LOCK:
        st = STATE
        u = ctx.user(st)
        return 200, {"user_id": u["id"], "display_name": u["display_name"],
                     "handle": u["handle"], "balance": u["balance"],
                     "currency": st.currency, "minor_units": st.minor_units}


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
        "created_at": fmt_ts(ts),
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
    if any(st.users[uid]["balance"] + d < 0 for uid, d in net.items()):
        raise ApiError(409, "insufficient_funds", "settlement is not affordable")
    sid = new_id("st")
    ts = st.now()
    # Apply the net movements in one step; record each transfer as a payment.
    for uid, d in net.items():
        st.users[uid]["balance"] += d
    payments = []
    for frm, to, amount, note, vis in plan:
        p = {"id": new_id("p"), "from_user_id": frm["id"], "to_user_id": to["id"],
             "amount": amount, "note": note, "visibility": vis, "request_id": None,
             "settlement_id": sid, "created_at": fmt_ts(ts), "ts": ts,
             "seq": st.next_seq()}
        st.payments[p["id"]] = p
        payments.append(p)
    st.settlements[sid] = {"id": sid, "operator_id": user["id"],
                           "payment_ids": [p["id"] for p in payments],
                           "committed_at": fmt_ts(ts)}
    return 201, {"settlement_id": sid, "committed_at": fmt_ts(ts),
                 "payments": [st.payment_view(p) for p in payments]}


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

REQ_ACTION_RE = re.compile(r"^/requests/([^/]+)/(pay|decline|cancel)$")


def route(ctx: Ctx):
    m, path = ctx.method, ctx.path
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
    match = REQ_ACTION_RE.match(path)
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
            query = parse_qs(parts.query, keep_blank_values=True)
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
            data = encode(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def do_GET(self):
        self._handle("GET")

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
