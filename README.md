# dataloupe

**See through any data file.** One command, zero dependencies, no setup.

[![tests](https://github.com/lilikk888/dataloupe/actions/workflows/tests.yml/badge.svg)](https://github.com/lilikk888/dataloupe/actions/workflows/tests.yml)
[![python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![dependencies](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](#)
[![license](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

![dataloupe output](https://raw.githubusercontent.com/lilikk888/dataloupe/main/docs/terminal.svg)

Point it at a CSV, TSV, JSONL, JSON, or XLSX file and it tells you what is
actually inside: the encoding, the delimiter, what each column really contains,
and — more importantly — **what is wrong with it**.

```bash
python dataloupe.py sales.csv
```

```
  dataloupe  v1.6.0

  users.csv
  CSV · UTF-8 · 逗号分隔 · 1.1KB
  10 行 × 7 列 · 用时 0.02s

  体检评分   52 / 100  ███████████░░░░░░░░░░░  问题较多

  问题：严重 4  警告 2  提示 0

  列名     类型  缺失  唯一值  内容摘要
  ───────  ────  ────  ──────  ──────────────────────────────────
  id       int     0%      10  1 ~ 10，均值 5.50
  name     text    0%      10  最常见「刘洋」10%
  email    text    0%      10  最常见「chenjing@example.…」10%
  phone    int     0%      10  13,000,130,000 ~ 13,912,345,678，…
  id_card  int     0%      10  110,101,199,003,070,016 ~ 510,101…
  address  text    0%      10  最常见「上海市浦东新区世…」10%
  note     text    0%       3  最常见「普通用户」70%

  ⚠ 疑似敏感信息  分享或入库前请先脱敏
  ● email: 邮箱 100%
  ● phone: 手机号 100%
  ● id_card: 身份证号 100%

  发现的问题
  ● 严重  疑似包含敏感信息
      「email」样本中有 邮箱 100% 命中
      → 确认该列是否含有 PII/密钥；在分享或入库前先做脱敏
  ● 严重  数值超出精确整数范围
      「id_card」最大值 510,101,199,512,080,064 超过 2^53，
      浮点数无法精确表示，末位数字已经丢了
      → 身份证号 / 银行卡号 / 长 ID 必须存成文本
```

Add `-o report.html` and you get a single, self-contained web report — charts,
column cards, and a prioritised list of fixes. No server, no CDN, no internet.

---

## Why another profiler?

`pandas-profiling` / `ydata-profiling` and `sweetviz` are good tools. They also
pull in pandas, numpy, matplotlib, and a few dozen transitive dependencies —
hundreds of megabytes — just to answer "is this CSV clean?".

`dataloupe` is **one file, standard library only**.

| | ydata-profiling / sweetviz | **dataloupe** |
|---|---|---|
| Install | `pip install` + pandas/numpy/matplotlib | **copy one file** |
| Dependencies | dozens | **zero** |
| Startup | several seconds | ~0.1s |
| Memory | grows with row count | **flat 76 MB** (50k rows to 5M) |
| 500k rows × 8 cols | minutes | **4.8s** (measured) |
| GBK / GB18030 input | often breaks | auto-detected |
| Reads .xlsx | needs openpyxl | built in (zip + xml) |
| Output | heavy HTML | single-file HTML, works offline |

It is not a replacement for a full profiling library. It is what you reach for
when you just got a file and want to know whether it is usable — before you
write a single line of analysis code.

---

## Quick start

```bash
# Terminal summary
python dataloupe.py data.csv

# Also write a standalone HTML report
python dataloupe.py data.csv -o report.html

# Machine-readable output for scripts and CI
python dataloupe.py data.csv --json

# Peek at the first rows while you are here
python dataloupe.py data.csv --preview

# Sample a huge file instead of reading all of it
python dataloupe.py huge.csv --limit 100000

# Emit a CREATE TABLE statement from the inferred types
python dataloupe.py data.csv --sql postgres

# Redact emails / phones / ID cards into a shareable copy
python dataloupe.py users.csv --mask

# Repair the data into a new file (the original is untouched)
python dataloupe.py data.csv --fix
```

Exit codes make it usable as a CI gate:

| Code | Meaning |
|---|---|
| `0` | no high-severity issues (and the score clears `--fail-under`) |
| `1` | high-severity issues found, or the score is below `--fail-under` |
| `2` | file missing or unreadable |

`--json` follows the same exit codes, so it works directly as a CI gate:

```yaml
# Fail the build when incoming data is broken
- run: python dataloupe.py data/export.csv --json

# Or gate on the score: anything under 85 fails
- run: python dataloupe.py data/export.csv --fail-under 85
```

---

## Clean it up

Diagnosis is half the job. `--fix` writes a repaired copy and leaves the
original untouched.

```bash
python dataloupe.py dirty.csv --fix
# -> dirty.cleaned.csv

python dataloupe.py dirty.csv --fix -o clean.csv --dedupe --drop-empty-cols
```

```
  已清洗 → dirty.cleaned.csv

  修正 51 个单元格（空格 39、全角 4）
  删除 6 行重复
  删除 1 个空列
  修正 2 个列名（空名或重名）
  写出 61 行 × 7 列
```

On the bundled `examples/messy.csv` this takes the score from **46 to 91** — the
remainder is missing data, which no tool should invent for you.

| Fix | Always on | Opt-in |
|---|---|---|
| Trim leading/trailing whitespace | ✓ | |
| Full-width ASCII → half-width (`１２３` → `123`) | ✓ | |
| Remove control characters | ✓ | |
| `NA` / `null` / `-` → empty | ✓ | |
| Empty and duplicate column names | ✓ | `--keep-headers` disables |
| Duplicate rows | | `--dedupe` |
| Fully empty columns | | `--drop-empty-cols` |

Output is UTF-8 **with a BOM** by default, so Excel opens it without mojibake.
Use `--no-bom` for pipelines that would choke on the marker.

It never guesses. Nothing is imputed, no rows are invented, and text that merely
looks numeric is left alone.

---

## CREATE TABLE, inferred

The inferred types turn straight into DDL, which skips the "read the data, then
hand-write the schema" step.

```bash
python dataloupe.py sales.csv --sql postgres
```

```sql
-- dataloupe 1.1.0 · sales.csv · 240 rows × 8 columns
CREATE TABLE sales (
    order_id    BIGINT,
    order_date  DATE,
    product     TEXT,
    category    TEXT,
    quantity    BIGINT,
    unit_price  DOUBLE PRECISION,
    customer    TEXT,
    region      TEXT
);
```

`postgres`, `mysql` and `sqlite` are supported, each with its own type names:

| | postgres | mysql | sqlite |
|---|---|---|---|
| integer | `BIGINT` | `BIGINT` | `INTEGER` |
| decimal | `DOUBLE PRECISION` | `DOUBLE` | `REAL` |
| boolean | `BOOLEAN` | `TINYINT(1)` | `INTEGER` |
| date | `DATE` / `TIMESTAMP` | `DATE` / `DATETIME` | `TEXT` |

A date column that also carries times is promoted to `TIMESTAMP`. Column names
that are not plain lowercase identifiers — `Order`, `客户名称`, the keyword
`user` — are quoted automatically, with backticks on MySQL. The table name
defaults to the file name; `--table events` overrides it.

---

## Drift: structure, distribution, rows

The quietest data-pipeline accident is an overnight export that changed shape:
downstream code either crashes or, worse, **silently computes the wrong answer**.
`--diff` takes two snapshots of what should be the same table and says what moved:

```bash
python dataloupe.py last_week.csv today.csv --diff

# ... or write a shareable HTML drift report
python dataloupe.py last_week.csv today.csv --diff -o drift.html

# Declare expectations; a broken contract exits 1
python dataloupe.py data.csv --require "id:not-null" --require "rows>=1000"

# Write a Markdown data dictionary / a score badge
python dataloupe.py data.csv --dict
python dataloupe.py data.csv --badge

# Write a Markdown data dictionary / a score badge
python dataloupe.py data.csv --dict
python dataloupe.py data.csv --badge
```

```text
  新增列（建议确认来源）
    + country

  疑似重命名（类型一致，名称相近）
    ~ price -> unit_price

  类型污染（混入无法解析的值）
    ! qty  0.0% -> 20.0%

  质量退化（缺失率上升）
    ↓ note  0.0% -> 40.0%

  分布漂移（数值中心明显移动）
    ≠ amount         均值 107.56 -> 403.86  (+275%)
    ≠ shipping_days  均值 2.93 -> 4.53  (+55%)

  行级差异  按共有的 5 列比对（不同行，非行数）
    + 2 行只出现在新文件
    - 2 行只出现在旧文件
      共有 3 行

  结论：破坏性变更。
```

![drift report](https://raw.githubusercontent.com/lilikk888/dataloupe/main/docs/diff.svg)

What it catches:

| Signal | Meaning |
|---|---|
| Added / removed columns | Code that reads by name breaks the moment a column vanishes |
| **Likely rename** | A near-name with the same type (`price` -> `unit_price`) is guessed as one rename, not a drop plus add |
| Type change | `int` -> `text` is breaking; `int` <-> `float` is treated as compatible |
| **Type pollution** | The inferred type stays put but unparseable values (`TBD`, `N/A`) creep in — the easiest one to miss |
| Quality regression | A column's missing rate climbs sharply |
| **Distribution drift** | The type did not change but the centre moved: mean shifted by ≥1 old standard deviation, or ≥50% of its old value |
| **Row-level diff** | Which rows exist only in the new file, which only in the old — data lost or data gained, at a glance |

Two yardsticks for distribution drift, either one sufficient: **effect size**
(the mean moved by at least one old standard deviation — scale-free, so revenue
and percentages behave the same) and **relative change** (at least half the old
mean — catches high-variance columns the effect-size test would wave through).
Auto-increment keys are excluded: their mean is a row counter, not a measurement.

The row diff compares **only the columns the two files share**. Otherwise an
added column makes every row look different and the noise buries the signal.
Each row costs one 64-bit fingerprint, capped at 500,000 per side (~30 MB); past
that the numbers are labelled as lower bounds. Counts are of *distinct* rows,
which is what a set comparison can honestly report.

Exit codes follow the CI gate: a **breaking** change (removed column, type
pollution, quality regression, encoding change) fails; only-added, a likely
rename, an `int`<->`float` widening, a distribution shift, or lost rows do not
(seasonality is not a bug). Add `--strict` to fail on any drift at all.
`--no-rows` skips the extra scan on huge files. `--diff --json` feeds a program.

---

## Masking: act on what was found

Detection without action is a report; `--mask` is the prescription. It rewrites
the sensitive values into masks and writes a copy that is safe to share:

```bash
python dataloupe.py users.csv --mask
# writes users.masked.csv, leaves the source untouched
```

| Kind | Original | Masked | Kept |
|---|---|---|---|
| Email | `zhangwei@example.com` | `z***@example.com` | the domain |
| Phone | `13800138000` | `138****8000` | first 3, last 4 |
| ID card | `110101199003071234` | `110101********1234` | region code, last 4 |
| Bank card | `4111 1111 1111 1111` | `************1111` | last 4 |
| IPv4 | `192.168.1.20` | `192.168.*.*` | first two octets |
| URL | `https://x.com/a?token=1` | `https://x.com/***` | scheme and host |

One deliberate restraint: **a bare 13-19 digit run is not treated as a card**,
because order numbers look exactly like card numbers. It is only masked when the
column itself was flagged, or the value is written with spaces or dashes;
`--mask-all` overrides that. Combine with `--fix` to clean and mask in one pass.

---

## Data contracts: write the requirement down

A profile tells you what is wrong with a file. A contract tells you whether it
meets *your* requirements. Those are different questions: data can be spotless
and still not have the thousand rows you expected.

```bash
python dataloupe.py orders.csv \
  --require "order_id:not-null" \
  --require "order_id:unique" \
  --require "amount:>0" \
  --require "status:in(paid|pending|refunded)" \
  --require "order_date:>=2024-01-01" \
  --require "rows>=1000"
```

```text
  dataloupe  v1.6.0  数据契约

  ✓ order_id:not-null        没有缺失值
  ✓ order_id:unique          240 个值全部唯一
  ✓ amount:>0                最小值 45.6，要求 >0
  ✗ status:in(paid|pending|refunded)  1 个值不符合（10.0%），例如 「试用用户」
  ✓ order_date:>=2024-01-01  最早 2026-03-01，要求 >=2024-01-01
  ✗ rows>=1000               rows 实际 240，要求 >=1000

  ✗ 2/6 条不通过。
```

![contract output](https://raw.githubusercontent.com/lilikk888/dataloupe/main/docs/rules.svg)

| Form | Meaning |
|---|---|
| `col:not-null` | No missing values in that column |
| `col:unique` | Every non-empty value is distinct |
| `col:>=N` `>N` `<=N` `<N` `=N` | Numeric bounds (checked against min/max) |
| `col:>=2024-01-01` | Date bound, compared as `YYYY-MM-DD` |
| `col:in(a\|b\|c)` | Every value is one of these |
| `col:matches(REGEX)` | Every value matches the pattern |
| `rows>=N` `cols<=N` | Table size |

**Any failure exits 1**, so it drops straight into CI with no config file:

```yaml
- run: python dataloupe.py data/export.csv --require "id:not-null" --require "rows>=1000"
```

Two deliberate choices:

- **A syntax mistake counts as a failure.** A typo in a contract must never look
  like a pass.
- **Comparing a text column with `>=` reports "not numeric"** instead of quietly
  succeeding.

`--json` puts the results under `rules`, so a pipeline can read them.

---

## Chinese data, handled properly

Most data tools are English-first and go quiet on this. dataloupe is built the
other way round:

| Capability | What it does |
|---|---|
| Encodings | GBK / GB18030 / Big5 detected automatically — no mojibake |
| Full-width chars | `１２３`, `ＡＢＣ` flagged: identical to the eye, wrong to a parser |
| Chinese headers | Tables align by East-Asian display width, so columns never drift |
| **ID card check digit** | GB 11643, weighted mod 11 — tells you the number is invented |
| **Unified credit code** | GB 32100-2015, mod 31 |
| **Organisation code** | mod 11 |
| **Licence plates** | Province character + issuing letter, 6-character new-energy too |
| **2^53 precision** | An 18-digit ID stored as a number has already lost its last digits |

A failed check digit is reported separately, because "the number is fake" and
"the number leaked" are different problems:

```text
● 警告  证件号校验位不通过
    「legal_person_id」7 个身份证号里有 2 个算不对校验位（29%）
    → 校验位错说明号码是编的或录入错了，不是真实证件号
```

`examples/china.csv` runs out of the box, with two deliberately broken check
digits.

---

## Data dictionary, badge, CI

**`--dict`** writes a Markdown data dictionary you can paste into a wiki:

```bash
dataloupe data.csv --dict        # -> data.dict.md
```

| Column | Type | Filled | Distinct | Range / most common | Notes |
|---|---|---|---|---|---|
| `email` | text | 100% | 10 | chenjing@example.com (1×) | sensitive: email |

**`--badge`** writes a score badge you can put in your own README:

```bash
dataloupe data.csv --badge       # -> data.badge.svg
```

**GitHub Action** — one line in a workflow (search "dataloupe" on Marketplace):

```yaml
- uses: lilikk888/dataloupe@main
  with:
    file: data/export.csv
    require: |
      id:not-null
      amount:>0
      rows>=1000
    fail-under: 85
```

---

## Correlations

Every pair of numeric columns gets a Pearson correlation. Pairs above |r| ≥ 0.5
are listed; anything at |r| ≥ 0.98 is flagged as **redundant**, because keeping
two copies of the same signal quietly distorts any model you train on it.

```
  强相关列  相关系数越接近 ±1 越像同一件事
  列 A      列 B       相关系数
  ────────  ─────────  ────────
  price     price_cny     +1.00
  area_sqm  price         +1.00
```

A set of three mutually correlated columns produces **one** finding, not three —
near-perfectly correlated columns are grouped with a union-find before reporting.

---

## What it checks

**Parsing**
- Encoding: UTF-8, UTF-8 with BOM, UTF-16, GB18030, GBK, Big5, CP1252
- Delimiter: comma, tab, semicolon, pipe — chosen by per-line consistency
- Ragged rows (field count ≠ header)
- Duplicate and empty column names
- **Names that differ only by case** (`id` and `ID` collide in SQL Server,
  Excel, and most ORMs)
- **CSV injection**: cells starting with `=` `+` `-` `@`, which Excel and WPS
  execute as formulas (`=cmd|'/C calc'!A0` is the classic payload). Plain
  numbers like `-5` are not flagged
- **Sensitive information (PII)**: email, phone, ID card, bank card (Luhn
  checked), IPv4, URL. A column only flags when 80%+ of its sample matches, so a
  stray value does not cause a false alarm; ID-card numbers are not also misread
  as bank cards

**Column types**
`int` · `float` · `bool` · `date` · `text` · `empty`, inferred with a 95%
tolerance so a handful of dirty cells do not flip a numeric column to text.

**Quality**
- Missing rate per column
- Fully duplicated rows
- Mixed-type columns (how many values do not match, with the first three offending values named)
- Leading/trailing whitespace
- Full-width ASCII (`１２３`, `ＡＢＣ`) that silently breaks parsing
- Invisible control characters
- Constant columns, near-unique ID columns, auto-increment sequences
- Outliers by the 1.5 × IQR rule
- **Date formats mixed inside one column** (`2024-01-05` next to `05/01/2024`)
- **Ambiguous dates** where day and month are both ≤ 12, so no tool can tell
  which is which
- Numeric distribution, mean/median/std, histograms

Every finding comes with a **suggested fix**, because "column X has 43% nulls"
is not actionable on its own.

---

## Supported formats

| Format | Extension | Notes |
|---|---|---|
| CSV | `.csv` `.txt` | delimiter auto-detected |
| TSV | `.tsv` | |
| JSON Lines | `.jsonl` `.ndjson` | keys unioned across records |
| JSON | `.json` | array of objects, or `{"data": [...]}` |
| Excel | `.xlsx` `.xlsm` | first worksheet, shared strings, dates via style |

`.xls` (the old binary format) is **not** supported — it is a different,
proprietary container. Re-save as `.xlsx` or CSV.

---

## The score

100 points, minus penalties:

| Severity | Penalty | Examples |
|---|---|---|
| high | −10 | empty column, duplicate column name, ragged rows, >50% missing |
| medium | −4 | duplicate rows, mixed types, 20–50% missing, control characters |
| low | −1 | stray spaces, full-width characters, constant column, outliers |

It is a rough signal, not a grade. A file scoring 70 with three low-severity
notes is usually fine; a file scoring 60 with one empty column is not.

---

## Install

```bash
pip install dataloupe
dataloupe data.csv
```

Or skip installing and run the single file directly:

```bash
python dataloupe.py data.csv
```

Python 3.9+. No dependencies at all — not "few", none.

---

## Performance and memory

Measured on Windows, Python 3.13, 8 mixed-type columns (int, date, text, float,
bool):

| Rows | File size | Time | Peak memory |
|---|---|---|---|
| 50,000 | 2.6 MB | 0.6 s | 47 MB |
| 500,000 | 26 MB | 4.8 s | 76 MB |
| 2,000,000 | 102 MB | 16.8 s | 76 MB |
| 5,000,000 | 257 MB | 39.4 s | 76 MB |

**A hundred times more rows, the same memory.** That is the main difference
between `dataloupe` and most profiling tools: it never loads the file. Rows are
folded into per-column accumulators as they arrive, then dropped.

```python
for row in rows:              # exactly one row in memory
    for acc, cell in zip(accumulators, row):
        acc.add(cell)
```

Every accumulator keeps only capped structures, so memory scales with the
number of columns, not rows:

| Kept | Cap |
|---|---|
| Distinct-value table | 100,000 values (reported as `≥100,000` past that) |
| Numeric sample | 200,000 floats (reservoir sampling, Algorithm L) |
| Type-voting sample | first 2,000 non-empty values |
| Hygiene sample | first 5,000 values |
| Duplicate-row fingerprints | first 200,000 distinct rows |

The trade-off: **past 200,000 numeric values, the median, quartiles and
histogram come from a random sample**, marked with `*` in the report. Min, max,
mean, standard deviation and missing counts stay exact at any size — mean and
variance are updated with Welford's recurrence, never stored-then-averaged.

Three things make it fast:

1. **ISO date fast path** — `datetime.strptime` compiles a regex and touches the
   locale on every call. Parsing 500k dates that way took 14 seconds; matching a
   single regex and constructing the `datetime` directly took under one.
2. **Algorithm L reservoir** — naive sampling calls the RNG once per row, which
   costs seconds on ten million rows. Algorithm L skips ahead geometrically, so
   the RNG is touched a few hundred thousand times.
3. **Sampled hygiene checks** — stray spaces, full-width characters and control
   characters are rare. They are detected on the first 5,000 values instead of
   every cell.

> `--fix` is the exception: it rewrites the whole table, so it still loads
> everything. On a huge file, look first with `--limit`, then decide.

---

## Tests

```bash
python -m unittest discover -s tests -v
```

196 tests covering encoding detection, delimiter sniffing, type inference,
statistics correctness, correlation, streaming-vs-one-shot equivalence,
reservoir accuracy, row-aligned correlation, streaming-vs-loading memory, CSV injection, DDL generation, exit codes, distribution drift,
row-level diff, masking, HTML drift reports, contract rules, bad-value samples, quality checks, cleaning, xlsx parsing, HTML escaping, and
two-file schema drift (added/removed columns, likely renames, type pollution, quality regression).

```
Ran 196 tests in 9.2s

OK
```

---

## License

MIT
