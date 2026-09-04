# Dataset Corpus Builder

This is a robust Flask microservice designed to validate, clean, deduplicate, and partition dataset files for machine learning training pipelines. It exposes a single endpoint: `POST /build-corpus`.

The service acts as a strict gatekeeper and processing engine for training data. When provided with a batch of dataset "objects" (JSONL files with associated metadata) and a time-window policy, it performs the following operations:

1. **Object Validation**: Verifies each file's metadata and contents, throwing out any file that is corrupted, has a mismatched CRC32C checksum, or does not conform to the expected schema.
2. **Canonicalization**: Cleans up the surviving rows (spacing, casing, timezones) so that two rows with equivalent meaning are represented identically.
3. **Deduplication**: Identifies and removes exact duplicates, keeping only the newest version of each record based on its revision number.
4. **Policy Enforcement**: Filters out records that fall outside the configured date range.
5. **Deterministic Splitting**: Sorts records into three buckets (train, validation, test) using a consistent hashing mechanism, ensuring the same entity always lands in the same bucket across runs.
6. **Contamination Check**: Analyzes validation and test rows to ensure they are not too similar to training rows. This prevents data leakage and ensures unbiased model evaluation.
7. **Deterministic Serialization**: Outputs the final partitioned data in a fixed, sorted order and computes SHA-256 hashes for each bucket, providing cryptographic proof of the output contents.

## Step-by-Step Processing

### 1. Request Validation
The endpoint strictly requires a JSON body containing a `policy` object and an `objects` array. 

### 2. Object-Level Validation
Each uploaded object undergoes rigorous checks:
- Proper URI format (`gs://bucket/path`)
- Generation matching (numeric equality check)
- CRC32C validation against the actual content
- Schema validation (requires `"training-v1"`)
- JSONL format validation per-line, ensuring required fields and correct data types

If any validation step fails, the entire object is rejected.

### 3. Canonicalization
- **Text & Entities**: Applies NFKC normalization, converts to lowercase, trims whitespace, and collapses continuous spaces into a single space.
- **Timestamps**: Converts all `eventTime` values to UTC and standardizes the format to `YYYY-MM-DDTHH:mm:ss.sssZ`.

### 4. Deduplication
Rows are grouped by canonical `(entity, eventTime, text)`. Conflicts are resolved by selecting the record with the highest `revision`, using the `id` as a fallback tiebreaker.

### 5. Time-Window Filtering
Records are evaluated against the provided `minTime` and `maxTime`. Out-of-bounds records are discarded.

### 6. Bucket Assignment
Records are routed to `train`, `validation`, or `test` partitions using modulo arithmetic on the first byte of the SHA-256 hash of the canonicalized `entity`.

### 7. Contamination (Leakage) Check
For every validation and test record, the service extracts word sets and compares them against every training record using **Jaccard similarity**. If the similarity meets or exceeds the configured `contaminationThreshold`, the record is dropped to prevent leakage.

### 8. Deterministic Output
Each split is sorted and re-serialized as compact JSON. The service produces SHA-256 digests for each partition, allowing consumers to verify data integrity.

## Files

- `app.py` — The core Flask service.
- `requirements.txt` — Project dependencies (Flask).

## Running Locally

```bash
pip install -r requirements.txt
python3 app.py            # listens on http://0.0.0.0:5000
curl -X POST http://localhost:5000/build-corpus \
     -H "Content-Type: application/json" \
     -d @sample_request.json
```
