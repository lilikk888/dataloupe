# Changelog

## 1.6.0

Chinese identity checks, a data dictionary, a score badge, and a GitHub Action.

**Check digits, not just patterns.** An 18-digit string that looks like an ID
card is not one. `dataloupe` now verifies:

| Kind | Standard | Test |
|---|---|---|
| 身份证号 | GB 11643 | weighted mod 11, `X` for 10 |
| 统一社会信用代码 | GB 32100-2015 | mod 31 over a 31-character alphabet |
| 组织机构代码 | — | mod 11, `X` for 10 |

All three are verified against published worked examples, not just against
themselves. A failed check digit is reported as **证件号校验位不通过** — a
medium-severity data-quality issue, separate from the PII finding, because "the
number is invented" and "the number leaked" are different problems. The 15-digit
legacy ID format has no check digit and is explicitly reported as unknown rather
than wrong.

**2^53.** An 18-digit ID stored as a number has already lost its last digits
before dataloupe ever reads it: float64 represents integers exactly only up to
2^53. Columns exceeding that now raise a high-severity issue explaining that the
damage happened at export time and cannot be undone downstream.

**`--dict`** writes a Markdown data dictionary — column, type, fill rate,
distinct count, range, and a note covering PII, type pollution, formula
injection and ambiguity. Made to be pasted into a wiki rather than read once.

**`--badge`** writes a shields-style SVG carrying the health score, so a project
can display the number next to its own README.

**`action.yml`** — a composite GitHub Action. One `uses:` line, with `require`,
`fail-under` and `report` inputs and a `score` output.

Also: numbers no longer print as `1.38001e+10` (a column of phone numbers used to
be unreadable); `--json -o` now writes the HTML report as well instead of
silently dropping it; `examples/china.csv`; 218 tests.

## 1.5.0

Two things: the report now names the cells to go fix, and `--require` turns
expectations into a gate.

**Offending values are shown, not just counted.** "12 values do not parse as
int" told you a number; it did not tell you which cells. The first three
unparseable values per column are now kept and printed:
`判定为 int，但有 3 个值不是（25.0%），例如 「TBD」、「N/A?」、「1 200」`.
Thousands separators are explicitly *not* treated as bad — `1,234` parses once
the separator is dropped, so it is a valid value.

**`--require RULE` declares a data contract.** Repeatable, and any failure
exits 1, so it works as a CI gate without a separate config file:

```bash
python dataloupe.py orders.csv \
  --require "order_id:not-null" \
  --require "order_id:unique" \
  --require "amount:>0" \
  --require "status:in(paid|pending|refunded)" \
  --require "order_date:>=2024-01-01" \
  --require "rows>=1000"
```

Supported: `not-null`, `unique`, numeric comparison (`>= > <= < =`), date
comparison against `YYYY-MM-DD`, `in(a|b|c)`, `matches(REGEX)`, and the
table-level `rows` / `cols` comparisons. Everything that can be answered from
the profile is; only `in(...)` and `matches(...)` need a pass over the values,
and all such rules share a single pass rather than one per rule.

A syntax mistake fails the rule instead of being ignored — a typo in a contract
must not look like a pass. Comparing a text column with `>=` reports "not
numeric" rather than silently passing.

`--json` carries the results under `rules`, so a pipeline can read them.

Also: 196 tests.

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
