# Changelog

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
