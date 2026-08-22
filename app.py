"""
build-corpus service
=====================

Implements POST /build-corpus exactly as described in the assignment spec:
  1. Validate + accept/reject each raw "object" (a JSONL file + its metadata).
  2. Canonicalize entity/text, normalize eventTime to UTC.
  3. Deduplicate rows by [entity, eventTime, text].
  4. Apply the time-window policy.
  5. Bucket rows into train / validation / test by a hash of entity.
  6. Reject validation/test rows that are "too similar" to a train row
     (leakage / contamination check).
  7. Sort + serialize each split deterministically and hash the bytes.

Everything is written to be deterministic: same input -> byte-identical
output, every time, on any machine.
"""

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request

app = Flask(__name__)

# --------------------------------------------------------------------------
# Constants: the exact reason codes the spec defines.
# --------------------------------------------------------------------------

OBJECT_CODES = {
    "URI_INVALID",
    "GENERATION_INVALID",
    "GENERATION_MISMATCH",
    "CRC32C_INVALID",
    "CRC32C_MISMATCH",
    "SCHEMA_INVALID",
    "JSONL_INVALID",
}

ROW_CODES = {
    "DUPLICATE",
    "POLICY_INVALID",
    "OUT_OF_WINDOW",
    "TRAIN_CONTAMINATION",
}

ROW_KEYS = ("id", "entity", "eventTime", "revision", "text")

# gs://<bucket>/<object-path>  (bucket: no slashes, object: anything after)
URI_RE = re.compile(r"^gs://[^/]+/.+$")

DECIMAL_RE = re.compile(r"^\d+$")

CRC32C_RE = re.compile(r"^[0-9a-f]{8}$")

# YYYY-MM-DDTHH:mm:ss[.sss](Z|+HH:mm|-HH:mm), fraction 1-3 digits
TIMESTAMP_RE = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"T(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?:\.(?P<frac>\d{1,3}))?"
    r"(?P<offset>Z|[+-]\d{2}:\d{2})$"
)

WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)  # letters/numbers, Unicode-aware


# --------------------------------------------------------------------------
# Low level helpers
# --------------------------------------------------------------------------

def crc32c_hex(data: bytes) -> str:
    """CRC32C (Castagnoli), returned as 8 lowercase hex digits."""
    poly = 0x82F63B78
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ poly
            else:
                crc >>= 1
    crc ^= 0xFFFFFFFF
    return format(crc, "08x")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canon_string(s: str) -> str:
    """NFKC normalize, lowercase, trim, collapse Unicode whitespace to one space."""
    s = unicodedata.normalize("NFKC", s)
    s = s.lower()
    s = s.strip()
    s = re.sub(r"\s+", " ", s, flags=re.UNICODE)
    return s


def compact_json(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def utf8_sort_key(s: str) -> bytes:
    return s.encode("utf-8")


def parse_timestamp(s: str):
    """
    Returns a timezone-aware UTC datetime + normalized string
    'YYYY-MM-DDTHH:mm:ss.sssZ', or (None, None) if invalid.
    """
    if not isinstance(s, str):
        return None, None
    m = TIMESTAMP_RE.match(s)
    if not m:
        return None, None

    year, month, day = int(m["year"]), int(m["month"]), int(m["day"])
    hour, minute, second = int(m["hour"]), int(m["minute"]), int(m["second"])
    frac = m["frac"] or "0"
    millis = int(frac.ljust(3, "0"))  # right-pad fraction digits to milliseconds

    offset = m["offset"]
    if offset == "Z":
        off_hours, off_minutes = 0, 0
        sign = 1
    else:
        sign = 1 if offset[0] == "+" else -1
        off_hours = int(offset[1:3])
        off_minutes = int(offset[4:6])

    # offset magnitude at most 14:00; hour 14 requires minutes 00
    if off_hours > 14 or (off_hours == 14 and off_minutes != 0) or off_minutes > 59:
        return None, None

    try:
        naive = datetime(year, month, day, hour, minute, second, millis * 1000)
    except ValueError:
        return None, None  # invalid calendar date/time (e.g. Feb 30, hour 24, etc.)

    offset_delta = timedelta(hours=off_hours, minutes=off_minutes) * sign
    aware_local = naive.replace(tzinfo=timezone(offset_delta))
    utc_dt = aware_local.astimezone(timezone.utc)

    normalized = utc_dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{utc_dt.microsecond // 1000:03d}Z"
    return utc_dt, normalized


def is_safe_nonneg_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= (2 ** 53 - 1)


def word_set(s: str):
    return set(w.lower() for w in WORD_RE.findall(s))


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


# --------------------------------------------------------------------------
# Row-shape validation (used while inspecting an object's JSONL content)
# --------------------------------------------------------------------------

def validate_row_shape(row) -> bool:
    """True iff `row` is a dict with exactly the 5 required keys, correct
    types, and a syntactically valid eventTime."""
    if not isinstance(row, dict):
        return False
    if set(row.keys()) != set(ROW_KEYS):
        return False
    if not isinstance(row["id"], str):
        return False
    if not isinstance(row["entity"], str):
        return False
    if not isinstance(row["text"], str):
        return False
    if not is_safe_nonneg_int(row["revision"]):
        return False
    utc_dt, _ = parse_timestamp(row["eventTime"])
    if utc_dt is None:
        return False
    return True


# --------------------------------------------------------------------------
# Object-level validation
# --------------------------------------------------------------------------

def validate_object(obj):
    """
    Returns (codes, uri_for_output, parsed_rows_or_None).
    parsed_rows_or_None is the list of row dicts IF and only IF the object
    ends up with zero codes (i.e. is accepted).
    """
    codes = set()

    raw_uri = obj.get("uri")
    uri_for_output = raw_uri if isinstance(raw_uri, str) else None
    if not isinstance(raw_uri, str) or not URI_RE.match(raw_uri):
        codes.add("URI_INVALID")

    gen = obj.get("generation")
    fgen = obj.get("fetchedGeneration")
    gen_ok = isinstance(gen, str) and DECIMAL_RE.match(gen)
    fgen_ok = isinstance(fgen, str) and DECIMAL_RE.match(fgen)
    if not gen_ok or not fgen_ok:
        codes.add("GENERATION_INVALID")
    elif int(gen) != int(fgen):
        codes.add("GENERATION_MISMATCH")

    crc = obj.get("crc32c")
    crc_ok = isinstance(crc, str) and CRC32C_RE.match(crc)
    if not crc_ok:
        codes.add("CRC32C_INVALID")

    content = obj.get("content")

    if crc_ok and isinstance(content, str):
        computed = crc32c_hex(content.encode("utf-8"))
        if computed != crc:
            codes.add("CRC32C_MISMATCH")

    if obj.get("schemaId") != "training-v1":
        codes.add("SCHEMA_INVALID")

    parsed_rows = []
    if not isinstance(content, str):
        codes.add("SCHEMA_INVALID")
    else:
        lines = content.split("\n")
        saw_json_error = False
        saw_shape_error = False
        non_blank_count = 0
        for line in lines:
            if line.strip() == "":
                continue
            non_blank_count += 1
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                saw_json_error = True
                continue
            if not validate_row_shape(row):
                saw_shape_error = True
                continue
            parsed_rows.append(row)

        if saw_json_error:
            codes.add("JSONL_INVALID")
        if non_blank_count == 0 or saw_shape_error:
            codes.add("SCHEMA_INVALID")

    if codes:
        return codes, uri_for_output, None
    return codes, uri_for_output, parsed_rows


# --------------------------------------------------------------------------
# Policy validation
# --------------------------------------------------------------------------

def validate_policy(policy):
    """Returns (is_valid, min_utc, max_utc, threshold)."""
    if not isinstance(policy, dict):
        return False, None, None, None

    min_raw = policy.get("minTime")
    max_raw = policy.get("maxTime")
    threshold = policy.get("contaminationThreshold")

    min_utc, _ = parse_timestamp(min_raw) if isinstance(min_raw, str) else (None, None)
    max_utc, _ = parse_timestamp(max_raw) if isinstance(max_raw, str) else (None, None)

    if min_utc is None or max_utc is None:
        return False, None, None, None

    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        return False, None, None, None
    if isinstance(threshold, float) and (threshold != threshold or threshold in (float("inf"), float("-inf"))):
        return False, None, None, None
    if not (0 <= threshold <= 1):
        return False, None, None, None

    return True, min_utc, max_utc, float(threshold)


# --------------------------------------------------------------------------
# Main endpoint
# --------------------------------------------------------------------------

@app.route("/build-corpus", methods=["POST"])
def build_corpus():
    body = request.get_json(silent=True, force=True)
    if not isinstance(body, dict):
        return jsonify({"error": "INVALID_INPUT"}), 400

    policy = body.get("policy")
    objects = body.get("objects")

    if not isinstance(policy, dict) or not isinstance(objects, list):
        return jsonify({"error": "INVALID_INPUT"}), 400

    rejected_objects = []
    lineage = []
    candidate_rows = []  # list of raw parsed row dicts from accepted objects

    for obj in objects:
        if not isinstance(obj, dict):
            rejected_objects.append({"uri": None, "reasonCodes": ["SCHEMA_INVALID"]})
            continue

        codes, uri_for_output, rows = validate_object(obj)
        if codes:
            rejected_objects.append(
                {"uri": uri_for_output, "reasonCodes": sorted(codes, key=utf8_sort_key)}
            )
        else:
            lineage.append(
                {
                    "uri": obj.get("uri"),
                    "generation": obj.get("generation"),
                    "crc32c": obj.get("crc32c"),
                    "schemaId": obj.get("schemaId"),
                }
            )
            candidate_rows.extend(rows)

    # ---- canonicalize -----------------------------------------------------
    canon_rows = []
    for row in candidate_rows:
        _, normalized_time = parse_timestamp(row["eventTime"])
        canon_rows.append(
            {
                "id": row["id"],
                "entity": canon_string(row["entity"]),
                "eventTime": normalized_time,
                "revision": row["revision"],
                "text": canon_string(row["text"]),
            }
        )

    # ---- deduplicate by [entity, eventTime, text] --------------------------
    groups = {}
    for row in canon_rows:
        key = (row["entity"], row["eventTime"], row["text"])
        groups.setdefault(key, []).append(row)

    rejected_rows = {}  # id -> set of codes

    def reject_row(row_id, code):
        rejected_rows.setdefault(row_id, set()).add(code)

    survivors = []
    for key, group in groups.items():
        if len(group) == 1:
            survivors.append(group[0])
            continue
        ordered = sorted(
            group, key=lambda r: (-r["revision"], utf8_sort_key(r["id"]))
        )
        winner = ordered[0]
        survivors.append(winner)
        for loser in ordered[1:]:
            reject_row(loser["id"], "DUPLICATE")

    # ---- policy / time window -----------------------------------------------
    policy_valid, min_utc, max_utc, threshold = validate_policy(policy)

    kept_after_window = []
    if not policy_valid:
        for row in survivors:
            reject_row(row["id"], "POLICY_INVALID")
    else:
        for row in survivors:
            row_utc, _ = parse_timestamp(row["eventTime"])
            if row_utc < min_utc or row_utc > max_utc:
                reject_row(row["id"], "OUT_OF_WINDOW")
            else:
                kept_after_window.append(row)

    # ---- bucket assignment ---------------------------------------------------
    train, validation, test = [], [], []
    for row in kept_after_window:
        digest = hashlib.sha256(row["entity"].encode("utf-8")).digest()
        bucket = digest[0] % 10
        if bucket <= 5:
            train.append(row)
        elif bucket <= 7:
            validation.append(row)
        else:
            test.append(row)

    # ---- contamination check (validation/test vs train) ----------------------
    train_word_sets = [word_set(r["text"]) for r in train]

    def passes_contamination(row):
        if threshold is None:
            return True
        ws = word_set(row["text"])
        for tws in train_word_sets:
            if jaccard(ws, tws) >= threshold:
                return False
        return True

    kept_validation = []
    for row in validation:
        if passes_contamination(row):
            kept_validation.append(row)
        else:
            reject_row(row["id"], "TRAIN_CONTAMINATION")

    kept_test = []
    for row in test:
        if passes_contamination(row):
            kept_test.append(row)
        else:
            reject_row(row["id"], "TRAIN_CONTAMINATION")

    # ---- sort + serialize each split ------------------------------------------
    def sort_key(row):
        return (utf8_sort_key(row["id"]), utf8_sort_key(compact_json(row)))

    def serialize_split(rows):
        rows_sorted = sorted(rows, key=sort_key)
        ordered_objs = [
            {
                "id": r["id"],
                "entity": r["entity"],
                "eventTime": r["eventTime"],
                "revision": r["revision"],
                "text": r["text"],
            }
            for r in rows_sorted
        ]
        lines = [compact_json(o) + "\n" for o in ordered_objs]
        blob = "".join(lines).encode("utf-8")
        return ordered_objs, sha256_hex(blob)

    train_out, train_digest = serialize_split(train)
    val_out, val_digest = serialize_split(kept_validation)
    test_out, test_digest = serialize_split(kept_test)

    # ---- assemble rejectedRows / lineage / rejectedObjects --------------------
    rejected_rows_list = [
        {"id": rid, "reasonCodes": sorted(codes, key=utf8_sort_key)}
        for rid, codes in rejected_rows.items()
    ]
    rejected_rows_list.sort(key=lambda r: (utf8_sort_key(r["id"]), utf8_sort_key(compact_json(r))))

    rejected_objects.sort(
        key=lambda o: (utf8_sort_key(o["uri"] or ""), utf8_sort_key(compact_json(o)))
    )

    lineage.sort(
        key=lambda o: (utf8_sort_key(o["uri"] or ""), utf8_sort_key(compact_json(o)))
    )

    response = {
        "splits": {"train": train_out, "validation": val_out, "test": test_out},
        "rejectedObjects": rejected_objects,
        "rejectedRows": rejected_rows_list,
        "digests": {"train": train_digest, "validation": val_digest, "test": test_digest},
        "lineage": lineage,
    }
    return app.response_class(
        response=json.dumps(response, ensure_ascii=False, separators=(",", ":")),
        status=200,
        mimetype="application/json",
    )


if __name__ == "__main__":
    import os

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
