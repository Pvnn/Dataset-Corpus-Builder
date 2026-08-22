# build-corpus — plain-English walkthrough

This is a tiny web server with one endpoint: `POST /build-corpus`. Think of it
as a very strict bouncer + filing clerk for training data. Someone hands it a
pile of "files" (JSONL text blobs) plus some rules. It:

1. **Checks each file's ID card** (rule 2 below) and throws out any file
   that's forged, corrupted, or the wrong shape.
2. **Cleans up the surviving rows** (spacing, casing, timezones) so two rows
   that "mean the same thing" look identical.
3. **Throws out exact duplicates**, keeping only the newest version of each.
4. **Throws out anything outside the allowed date range.**
5. **Sorts rows into 3 buckets** (train / validation / test) using a coin-flip
   that's actually a hash, so it's always the same for the same input.
6. **Checks for cheating**: if a validation/test row looks too similar to a
   training row, it gets thrown out too (otherwise the model could "peek" at
   the answer during evaluation — this is called leakage).
7. **Writes everything out in a fixed order** and fingerprints (SHA-256
   hashes) each bucket, so anyone can verify the output wasn't tampered with.

## Step-by-step, matching the code

### Step 1 — Is the request even shaped right?
`app.py`'s `build_corpus()` first checks the JSON body has a `policy` object
and an `objects` array. If not: HTTP 400, `{"error":"INVALID_INPUT"}`. No
exceptions, no partial credit — this is a hard gate.

### Step 2 — Validate every uploaded "object" (a file + its metadata)
`validate_object()` runs a checklist on each file, collecting **every**
problem it finds (not just the first one):

- Is the `uri` shaped like `gs://bucket/path`? → else `URI_INVALID`
- Are `generation`/`fetchedGeneration` plain digit-strings? → else
  `GENERATION_INVALID`. If both are valid digit-strings but don't match
  numerically → `GENERATION_MISMATCH`.
- Is `crc32c` 8 lowercase hex digits? → else `CRC32C_INVALID`. If it's
  syntactically fine AND `content` is a string, we recompute the CRC32C of
  the content ourselves (`crc32c_hex()`) and compare — mismatch →
  `CRC32C_MISMATCH`.
- Is `schemaId` exactly `"training-v1"`? → else `SCHEMA_INVALID`.
- Is `content` a string? If not → `SCHEMA_INVALID`. If yes, we split it into
  lines, ignore blank ones, and `json.loads()` each remaining line.
  - A line that isn't valid JSON → `JSONL_INVALID`.
  - A parsed line that isn't exactly
    `{id, entity, eventTime, revision, text}` with the right types (and a
    real, valid `eventTime`) → `SCHEMA_INVALID`.
  - Zero non-blank lines at all → `SCHEMA_INVALID`.

If **any** code got triggered, the whole file is rejected — none of its rows
are used, even the ones that individually looked fine. Otherwise, the file's
rows go into the pool for the next steps, and we remember its metadata as
"lineage" (proof of where the accepted data came from).

### Step 3 — Canonicalize (make equivalent things look identical)
`canon_string()` does 4 things to `entity` and `text`:
NFKC-normalize (folds visually-identical Unicode characters together),
lowercase, trim, and collapse any run of whitespace to a single space.

`parse_timestamp()` converts every `eventTime` to UTC and prints it back out
in one exact format: `YYYY-MM-DDTHH:mm:ss.sssZ`. So `+05:30` and `Z` times
that represent the same instant end up as the *same string*.

### Step 4 — Remove duplicates
Rows are grouped by the triple `(entity, eventTime, text)` — after
canonicalization. Within a group, we keep the row with the **highest
`revision`**; if there's a tie, we keep whichever `id` comes first when
compared byte-by-byte as UTF-8. Every other row in that group is thrown into
`rejectedRows` with reason `DUPLICATE`.

### Step 5 — Apply the policy (date window)
`validate_policy()` checks the policy itself is sane (`minTime`/`maxTime`
parse as real timestamps, `contaminationThreshold` is a real number in
`[0, 1]`). If the policy is broken, *every single row* gets rejected with
`POLICY_INVALID` — garbage-in, garbage-out. If the policy is fine, any row
whose `eventTime` falls outside `[minTime, maxTime]` gets rejected with
`OUT_OF_WINDOW`.

### Step 6 — Bucket into train / validation / test
For each surviving row: SHA-256 hash the (canonicalized) `entity` string,
look at the **first byte** of that hash, and take it `mod 10`:
`0–5 → train`, `6–7 → validation`, `8–9 → test`. Same entity always lands in
the same bucket, which keeps all of one entity's data on one side of the
split (a sane thing to want, so no entity is training and test at once).

### Step 7 — Contamination / leakage check
For every validation/test row, we turn its `text` into a set of lowercase
"words" (`word_set()` — runs of Unicode letters/digits). We compare it
against the word-set of *every* train row using **Jaccard similarity**
(`intersection size / union size`, and `1.0` if both sets are empty). If the
similarity is `≥ contaminationThreshold` for *any* train row, that
validation/test row is dropped as `TRAIN_CONTAMINATION` (it's too close to
something the model will have trained on).

### Step 8 — Deterministic output
Each split is sorted by the UTF-8 bytes of `id` (ties broken by comparing the
compact JSON of the row). Rows are re-serialized as compact JSON with an
**exact** key order (`id, entity, eventTime, revision, text`), one row per
line, non-ASCII characters written out directly (not escaped). All those
bytes, concatenated, get SHA-256'd — that's the `digest` for the split. Same
input → byte-identical output → same digest, forever.

## Files

- `app.py` — the whole service (single file, stdlib + Flask only).
- `requirements.txt` — just `flask`.

## Running it locally

```bash
pip install -r requirements.txt
python3 app.py            # listens on http://0.0.0.0:5000
curl -X POST http://localhost:5000/build-corpus \
     -H "Content-Type: application/json" \
     -d @sample_request.json
```

## Getting a public URL for the grader

I can't host a public URL for you from here — but this is a plain Flask app,
so any of these will work in a few minutes:

- **Render / Railway / Fly.io**: push these two files to a repo, connect it,
  and they'll give you a public HTTPS URL. Set the start command to
  `python3 app.py` (it already reads `PORT` from the environment).
- **A VM/VPS you already have**: `pip install flask`, run `python3 app.py`,
  open the port in your firewall, use `http://<your-ip>:5000` (or put it
  behind nginx/Caddy for HTTPS).

Whatever URL you get, that's what you paste into the "Public service base
URL" field — the grader will `POST` to `<that-url>/build-corpus`.

## Assumptions worth knowing about (the spec is ambiguous in a couple of spots)

- I treated `gs://bucket/object` as a **pattern** (`gs://<any-bucket>/<any-path>`),
  not a literal string — otherwise every real request would be rejected. Easy
  one-line change (`URI_RE`) if you actually want a literal match.
- `generation` equality is compared **numerically** (so `"007"` == `"7"`);
  switch to a plain string compare in `validate_object()` if the grader wants
  exact string equality instead.
- The word-tokenizer for the contamination check treats any run of Unicode
  letters/digits as one "word" (regex `[^\W_]+`), which matches the "letter/
  number word-set" wording as literally as I could.
