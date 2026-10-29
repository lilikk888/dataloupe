# Changelog

## 1.4.0

Three additions that turn `--diff` into a real pipeline check, plus the ability
to act on what the PII scan finds.

**Row-level diff.** `--diff` now reports how many rows exist only in the old
file, only in the new one, and in both. Comparison is restricted to the columns
the two files share, so an added or dropped column cannot make every row look
different — the noise that swamps a naive whole-row comparison. Rows are held as
one 64-bit fingerprint each, capped at 500,000 per side, so a million-row export
costs tens of MB rather than gigabytes; past the cap the numbers are labelled as
lower bounds. Counts are of *distinct* rows, which is what a set comparison can
honestly report.

**Distribution drift.** A numeric column whose centre moved is a change even
when its type did not. Two yardsticks, either sufficient: the mean moved by at
least one old standard deviation (scale-free), or by at least half its old value
(catches large relative moves in high-variance columns). Auto-increment keys are
excluded — their mean is a row counter, not a measurement. Reported as drift,
not as breaking: seasonality is not a bug, and `--strict` is the gate for teams
that want it to fail.

**`--mask` writes a redacted copy.** Detection that cannot act is a report; this
acts. Emails keep their domain (`z***@example.com`), phones keep the last four
(`138****8000`), ID cards keep both ends (`110101********1234`), bank cards keep
the last four, IPv4 keeps two octets, URLs keep the host. A bare 13-19 digit run
is only treated as a card when the column was flagged or the value is written
with separators, because order numbers look exactly like card numbers otherwise;
`--mask-all` overrides that. The source file is never modified.

Also: `--no-rows` to skip the extra scan, `examples/orders_march.csv` /
`orders_april.csv` to demonstrate distribution drift, and 22 new tests.

## 1.3.0

PII / sensitive-information detection.

**`dataloupe` now scans every column for patterns that should not be shared or
loaded into a warehouse un-redacted:** email, phone number, ID card, bank card
(verified with the Luhn checksum so a random 16-digit string is not flagged),
IPv4 address, and URL. The scan runs on the raw cell strings of *every* column,
not just text ones — a phone number stored as a bare 11-digit string lives in an
`int` column and would slip past a text-only check otherwise.

Precision matters for a feature like this, so a column only flags when **80%+ of
its sampled cells match**, and the sample is capped at 200 values regardless of
file size. A single stray email in a name column therefore stays quiet, while a
column that *is* emails is caught. Two correctness traps were closed here:

- An 18-digit ID card passes Luhn about 1 time in 10, so it was being double-flagged
  as a bank card. The bank-card check now skips any cell that already matched the
  ID-card pattern.
- Raw samples were only being exposed on text columns, so int columns showed no
  findings at all. The sample is now exported for every column kind.

`examples/users.csv` demonstrates the feature.

## 1.2.0

Two-file schema drift detection, plus input hardening.

**`--diff OLD NEW` compares two exports of the same table and reports what
moved.** This is the change that breaks a pipeline without anyone noticing: a
column renamed, a column dropped, a numeric column that turned into text
because someone typed "TBD" into it, a date column that stopped parsing. The
check catches:

- Columns added and removed.
- **Likely renames** — a near name with the same inferred type (`price` ->
  `unit_price`) is guessed as one rename rather than a drop plus add. Names must
  share a token (containment) or be within two edits once both are reasonably
  long, to avoid false positives.
- **Type pollution** — the inferred type stays put, but the share of values that
  no longer parse as that type jumps (a "N/A" that was not a missing token, a
  stray letter). The kind does not change, so a plain type-equality check would
  miss it; the mismatch rate is tracked separately.
- Quality regressions — a column's missing rate climbs.
- Encoding changes.

Exit codes follow the CI gate: a **breaking** change (removed column, type
pollution, quality regression, encoding change) fails; only-added, a likely
rename, or an `int` <-> `float` widening does not. `--strict` fails on any drift.
`--diff --json` emits machine-readable output.

**Fixes**

- A header row with no data, a flat JSON array, an empty file, and `--fix`
  pointed at its own input (which used to clobber the source) are now handled
  instead of crashing.
- Rename detection no longer pairs two single-character columns that simply
  differ in one letter.

## 1.1.0

Streaming rewrite, plus three new findings.

**Memory no longer follows the file size.** Rows are folded into per-column
accumulators and dropped, instead of being collected into one big list. A
257 MB / 5-million-row CSV now peaks at 76 MB, the same as a 500,000-row one.
Mean and standard deviation stay exact at any size (Welford); past 200,000
values the median, quartiles and histogram come from a uniform sample and the
report says so.

**Fixes**

- Correlations paired rows from different columns. Each column sampled its own
  values, so a gap in one column shifted it against its neighbour and the
  coefficient described rows that never existed. The sample is now drawn once
  per row.
- A CSV cell longer than 128 KiB raised `_csv.Error`; the stdlib field limit is
  now lifted.
- `--json` always exited 0, which broke the documented CI gate.
- Writing to a path whose folder does not exist crashed with a traceback; the
  folder is now created.
- Headers that differ only by case (`id` / `ID`) were not reported.

**New**

- `--sql DIALECT` emits a `CREATE TABLE` statement (`postgres` / `mysql` /
  `sqlite`), with `--table` to name it.
- CSV injection detection: cells a spreadsheet would execute as formulas.
- `--fail-under SCORE` for score-based CI gates.
- The distinct-value table frees its memory once capped; counts are reported as
  `≥100,000`.

## 1.0.0

First release.

- Reads CSV, TSV, JSONL, JSON and XLSX with the standard library alone.
- Encoding detection (UTF-8, UTF-8 BOM, UTF-16, GB18030, GBK, Big5, CP1252) and
  delimiter sniffing by per-line consistency.
- Type inference with a 95% tolerance, so a few dirty cells do not flip a
  numeric column to text.
- 0–100 health score with severity-weighted penalties.
- Terminal, HTML and JSON output; `--fix` writes a cleaned copy.
- Pearson correlation with union-find grouping of redundant column sets.
- Mixed and ambiguous date format detection.
