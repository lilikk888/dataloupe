"""Test suite for dataloupe. Run with: python -m unittest discover -s tests -v"""

import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dataloupe as dl


class TestTextWidth(unittest.TestCase):
    def test_ascii(self):
        self.assertEqual(dl.display_width("abc"), 3)

    def test_cjk_is_double_width(self):
        self.assertEqual(dl.display_width("中文"), 4)

    def test_mixed(self):
        self.assertEqual(dl.display_width("a中b"), 4)

    def test_pad_aligns_cjk(self):
        self.assertEqual(dl.display_width(dl.pad("中文", 10)), 10)
        self.assertEqual(dl.display_width(dl.pad("abcdef", 10, "right")), 10)

    def test_truncate_respects_width(self):
        result = dl.truncate("这是一个很长的中文句子", 8)
        self.assertLessEqual(dl.display_width(result), 8)
        self.assertTrue(result.endswith("…"))


class TestEncoding(unittest.TestCase):
    def _write(self, content: bytes) -> Path:
        handle = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
        handle.write(content)
        handle.close()
        self.addCleanup(Path(handle.name).unlink, missing_ok=True)
        return Path(handle.name)

    def test_utf8(self):
        path = self._write("城市,人口\n北京,2189\n".encode("utf-8"))
        self.assertEqual(dl.detect_encoding(path), "utf-8")

    def test_utf8_bom(self):
        path = self._write("\ufeff城市\n北京\n".encode("utf-8"))
        self.assertEqual(dl.detect_encoding(path), "utf-8-sig")

    def test_gbk(self):
        path = self._write("城市,人口\n北京,2189\n".encode("gb18030"))
        self.assertEqual(dl.detect_encoding(path), "gb18030")

    def test_empty_file(self):
        path = self._write(b"")
        self.assertEqual(dl.detect_encoding(path), "utf-8")


class TestDelimiter(unittest.TestCase):
    def test_comma(self):
        self.assertEqual(dl.sniff_delimiter("a,b,c\n1,2,3\n"), ",")

    def test_tab(self):
        self.assertEqual(dl.sniff_delimiter("a\tb\tc\n1\t2\t3\n"), "\t")

    def test_semicolon(self):
        self.assertEqual(dl.sniff_delimiter("a;b;c\n1;2;3\n"), ";")

    def test_single_column_falls_back_to_comma(self):
        self.assertEqual(dl.sniff_delimiter("a\nb\nc\n"), ",")


class TestTypeInference(unittest.TestCase):
    def test_classify_int(self):
        self.assertEqual(dl.classify_value("42"), "int")
        self.assertEqual(dl.classify_value("-7"), "int")

    def test_classify_float(self):
        self.assertEqual(dl.classify_value("3.14"), "float")
        self.assertEqual(dl.classify_value("1e5"), "float")

    def test_classify_bool(self):
        self.assertEqual(dl.classify_value("true"), "bool")
        self.assertEqual(dl.classify_value("No"), "bool")

    def test_classify_date(self):
        self.assertEqual(dl.classify_value("2024-01-15"), "date")
        self.assertEqual(dl.classify_value("2024-01-15T10:30:00Z"), "date")

    def test_classify_text(self):
        self.assertEqual(dl.classify_value("hello"), "text")
        self.assertEqual(dl.classify_value("北京"), "text")

    def test_int_column(self):
        kind, mismatch = dl.infer_kind(["1", "2", "3", "4"])
        self.assertEqual(kind, "int")
        self.assertEqual(mismatch, 0)

    def test_mixed_numeric_becomes_float(self):
        kind, _ = dl.infer_kind(["1", "2.5", "3", "4"])
        self.assertEqual(kind, "float")

    def test_tolerance_accepts_few_dirty_values(self):
        # "oops" is a genuine bad value, not a missing marker (those are filtered out).
        values = [str(i) for i in range(100)] + ["oops"]
        kind, mismatch = dl.infer_kind(values)
        self.assertEqual(kind, "int")
        self.assertGreater(mismatch, 0)

    def test_all_empty(self):
        self.assertEqual(dl.infer_kind(["", "  ", "NA"])[0], "empty")


class TestStatistics(unittest.TestCase):
    def test_percentile_median(self):
        self.assertAlmostEqual(dl.percentile([1, 2, 3, 4, 5], 0.5), 3.0)

    def test_percentile_quartiles(self):
        values = [1, 2, 3, 4, 5]
        self.assertAlmostEqual(dl.percentile(values, 0.25), 2.0)
        self.assertAlmostEqual(dl.percentile(values, 0.75), 4.0)

    def test_percentile_single(self):
        self.assertEqual(dl.percentile([7], 0.5), 7)

    def test_histogram_sums_to_count(self):
        counts, edges = dl.histogram([float(i) for i in range(100)])
        self.assertEqual(sum(counts), 100)
        self.assertEqual(len(edges), len(counts) + 1)

    def test_histogram_constant_column(self):
        counts, _ = dl.histogram([5.0, 5.0, 5.0])
        self.assertEqual(counts, [3])


class TestColumnProfile(unittest.TestCase):
    def test_numeric_stats(self):
        col = dl.profile_column("n", 0, ["1", "2", "3", "4", "100"])
        self.assertEqual(col.kind, "int")
        self.assertEqual(col.num_min, 1)
        self.assertEqual(col.num_max, 100)
        self.assertAlmostEqual(col.num_mean, 22.0)
        self.assertAlmostEqual(col.num_median, 3.0)
        self.assertEqual(col.distinct, 5)

    def test_outliers_detected(self):
        values = [str(i) for i in range(100)] + ["10000"]
        col = dl.profile_column("n", 0, values)
        self.assertGreaterEqual(col.num_outliers, 1)

    def test_missing_counted(self):
        col = dl.profile_column("n", 0, ["1", "", "NA", "4"])
        self.assertEqual(col.missing, 2)
        self.assertEqual(col.total, 4)
        self.assertAlmostEqual(col.missing_rate, 0.5)

    def test_text_top_values(self):
        col = dl.profile_column("t", 0, ["a", "a", "b", "c"])
        self.assertEqual(col.kind, "text")
        self.assertEqual(col.top_values[0], ("a", 2))

    def test_date_range(self):
        col = dl.profile_column("d", 0, ["2024-01-15", "2023-06-01", "2024-03-09"])
        self.assertEqual(col.kind, "date")
        self.assertEqual(col.date_min, "2023-06-01")
        self.assertEqual(col.date_max, "2024-03-09")

    def test_fullwidth_detected_but_not_han(self):
        col = dl.profile_column("t", 0, ["１２３", "普通中文"])
        self.assertEqual(col.fullwidth, 1)

    def test_leading_space_counted(self):
        col = dl.profile_column("t", 0, ["  a", "b", "c "])
        self.assertEqual(col.leading_space, 1)
        self.assertEqual(col.trailing_space, 1)


class TestAnalysis(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _csv(self, name: str, content: str, encoding: str = "utf-8") -> Path:
        path = self.dir / name
        path.write_text(content, encoding=encoding)
        return path

    def test_clean_file_scores_high(self):
        path = self._csv("clean.csv", "a,b\n1,x\n2,y\n3,z\n")
        profile = dl.analyse(path)
        self.assertEqual(profile.rows, 3)
        self.assertEqual(profile.cols, 2)
        self.assertGreaterEqual(profile.score, 90)

    def test_duplicate_rows_detected(self):
        path = self._csv("dup.csv", "a,b\n1,x\n1,x\n1,x\n2,y\n")
        profile = dl.analyse(path)
        self.assertEqual(profile.duplicate_rows, 2)
        titles = [i.title for i in profile.issues]
        self.assertIn("存在重复行", titles)

    def test_duplicate_column_names_flagged(self):
        path = self._csv("duph.csv", "a,a,b\n1,2,3\n")
        profile = dl.analyse(path)
        titles = [i.title for i in profile.issues]
        self.assertIn("列名重复", titles)

    def test_empty_column_name_flagged(self):
        path = self._csv("emptyh.csv", "a,,c\n1,2,3\n")
        profile = dl.analyse(path)
        self.assertIn("列名为空", [i.title for i in profile.issues])

    def test_ragged_rows_detected(self):
        path = self._csv("ragged.csv", "a,b,c\n1,2,3\n4,5\n6,7,8\n")
        profile = dl.analyse(path)
        self.assertEqual(profile.ragged_rows, 1)
        self.assertIn("行列数不一致", [i.title for i in profile.issues])

    def test_gbk_file_readable(self):
        path = self._csv("gbk.csv", "城市,人口\n北京,2189\n上海,2487\n", encoding="gb18030")
        profile = dl.analyse(path)
        self.assertEqual(profile.columns[0].name, "城市")
        self.assertEqual(profile.rows, 2)

    def test_tsv_detected(self):
        path = self._csv("data.tsv", "a\tb\n1\t2\n")
        profile = dl.analyse(path)
        self.assertEqual(profile.fmt, "tsv")
        self.assertEqual(profile.cols, 2)

    def test_jsonl(self):
        path = self.dir / "e.jsonl"
        path.write_text(
            "\n".join(json.dumps(r) for r in [{"a": 1, "b": "x"}, {"a": 2, "c": "y"}]),
            encoding="utf-8",
        )
        profile = dl.analyse(path)
        self.assertEqual(profile.fmt, "jsonl")
        self.assertEqual(profile.cols, 3)
        self.assertEqual(profile.rows, 2)

    def test_json_array(self):
        path = self.dir / "a.json"
        path.write_text(json.dumps([{"a": 1}, {"a": 2}]), encoding="utf-8")
        profile = dl.analyse(path)
        self.assertEqual(profile.fmt, "json")
        self.assertEqual(profile.rows, 2)

    def test_limit_truncates(self):
        rows = "\n".join(f"{i},x" for i in range(100))
        path = self._csv("big.csv", f"a,b\n{rows}\n")
        profile = dl.analyse(path, limit=10)
        self.assertEqual(profile.rows, 10)
        self.assertTrue(profile.truncated)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            dl.analyse(self.dir / "nope.csv")

    def test_empty_file_raises(self):
        path = self._csv("empty.csv", "")
        with self.assertRaises(ValueError):
            dl.analyse(path)


class TestXlsx(unittest.TestCase):
    def _build(self, rows, date_col=None) -> Path:
        def col_letter(i):
            out, i = "", i + 1
            while i:
                i, rem = divmod(i - 1, 26)
                out = chr(65 + rem) + out
            return out

        def sheet(rows):
            parts = ['<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
            for r, row in enumerate(rows, 1):
                cells = []
                for c, value in enumerate(row):
                    ref = f"{col_letter(c)}{r}"
                    if date_col is not None and c == date_col and r > 1:
                        cells.append(f'<c r="{ref}" s="1"><v>45300</v></c>')
                    elif isinstance(value, (int, float)):
                        cells.append(f'<c r="{ref}"><v>{value}</v></c>')
                    else:
                        cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>')
                parts.append(f'<row r="{r}">{"".join(cells)}</row>')
            parts.append("</sheetData></worksheet>")
            return "".join(parts)

        path = Path(tempfile.mktemp(suffix=".xlsx"))
        self.addCleanup(path.unlink, missing_ok=True)
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("[Content_Types].xml",
                       '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                       '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                       '<Default Extension="xml" ContentType="application/xml"/></Types>')
            z.writestr("_rels/.rels",
                       '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
            z.writestr("xl/workbook.xml",
                       '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                       '<sheets><sheet name="S" sheetId="1" r:id="rId1" '
                       'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/></sheets></workbook>')
            z.writestr("xl/styles.xml",
                       '<?xml version="1.0"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                       '<numFmts count="0"/><cellXfs count="2">'
                       '<xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>')
            z.writestr("xl/worksheets/sheet1.xml", sheet(rows))
        return path

    def test_reads_values(self):
        path = self._build([["name", "qty"], ["apple", 3], ["pear", 5]])
        header, rows = dl.read_xlsx(path)
        self.assertEqual(header, ["name", "qty"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], ["apple", "3"])

    def test_date_serial_converted(self):
        path = self._build([["item", "when"], ["a", "x"], ["b", "y"]], date_col=1)
        _, rows = dl.read_xlsx(path)
        self.assertTrue(rows[0][1].startswith("2024-"), rows[0][1])

    def test_gap_column_preserved(self):
        path = self._build([["a", "c"], ["1", "3"]])
        header, rows = dl.read_xlsx(path)
        self.assertEqual(len(header), 2)
        self.assertEqual(rows[0], ["1", "3"])


class TestRendering(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        path = self.dir / "d.csv"
        path.write_text("name,age,city\nAlice,30,Beijing\nBob,,Shanghai\n", encoding="utf-8")
        self.profile = dl.analyse(path)

    def test_terminal_report_contains_key_parts(self):
        text = dl.render_terminal(self.profile)
        self.assertIn("dataloupe", text)
        self.assertIn("体检评分", text)
        self.assertIn("Alice", dl.render_terminal(self.profile, show_preview=True))

    def test_html_is_self_contained(self):
        html = dl.render_html(self.profile)
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        self.assertIn("</html>", html)
        # No external resources: the report must work offline.
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)

    def test_html_escapes_special_characters(self):
        path = self.dir / "x.csv"
        path.write_text('a,b\n"<script>","&"\n', encoding="utf-8")
        html = dl.render_html(dl.analyse(path))
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)

    def test_json_output_is_valid(self):
        payload = json.loads(dl.render_json(self.profile))
        self.assertEqual(payload["rows"], 2)
        self.assertEqual(payload["cols"], 3)
        self.assertIn("score", payload)


class TestOutputEncoding(unittest.TestCase):
    """Regression tests for the cp1252 crash on English Windows installs."""

    def test_configure_output_survives_streams_without_reconfigure(self):
        import io
        original = sys.stdout
        sys.stdout = io.StringIO()  # StringIO has no .reconfigure
        try:
            dl._configure_output()  # must not raise
        finally:
            sys.stdout = original

    def test_chinese_report_is_encodable_as_utf8(self):
        # The report contains Chinese labels; it must always be encodable.
        profile = dl.analyse(Path(__file__).resolve().parent.parent / "examples" / "sales.csv")
        text = dl.render_terminal(profile)
        self.assertTrue(text.encode("utf-8"))


class TestDateFormatDetection(unittest.TestCase):
    def test_iso(self):
        self.assertEqual(dl.detect_date_format("2024-01-05"), "YYYY-MM-DD")

    def test_iso_with_time(self):
        self.assertIn("HH:MM:SS", dl.detect_date_format("2024-01-05 10:30:00"))

    def test_slash_year_first(self):
        self.assertEqual(dl.detect_date_format("2024/01/05"), "YYYY/MM/DD")

    def test_day_first(self):
        self.assertEqual(dl.detect_date_format("05/03/2024"), "DD/MM/YYYY")

    def test_ambiguous_slashed_date_prefers_day_first(self):
        # 03/05/2024 could be either 3 May or 5 March. We resolve it as DD/MM
        # (European) and report the ambiguity separately rather than guessing.
        self.assertEqual(dl.detect_date_format("03/05/2024"), "DD/MM/YYYY")

    def test_unambiguous_day_first(self):
        self.assertEqual(dl.detect_date_format("25/03/2024"), "DD/MM/YYYY")

    def test_ambiguous_detection(self):
        self.assertTrue(dl.is_ambiguous_date("03/05/2024"))
        self.assertTrue(dl.is_ambiguous_date("11-12-2024"))
        self.assertFalse(dl.is_ambiguous_date("25/03/2024"))
        self.assertFalse(dl.is_ambiguous_date("2024-01-05"))

    def test_chinese_date(self):
        self.assertEqual(dl.detect_date_format("2024年1月5日"), "YYYY年MM月DD日")

    def test_not_a_date(self):
        self.assertIsNone(dl.detect_date_format("hello"))

    def test_mixed_formats_flagged_as_high(self):
        path = Path(tempfile.mktemp(suffix=".csv"))
        self.addCleanup(path.unlink, missing_ok=True)
        path.write_text(
            "id,when\n1,2024-01-05\n2,2024-02-11\n3,05/03/2024\n4,2024-04-20\n",
            encoding="utf-8",
        )
        profile = dl.analyse(path)
        mixed = [i for i in profile.issues if i.title == "日期格式混用"]
        self.assertEqual(len(mixed), 1)
        self.assertEqual(mixed[0].severity, "high")

    def test_single_format_not_flagged(self):
        path = Path(tempfile.mktemp(suffix=".csv"))
        self.addCleanup(path.unlink, missing_ok=True)
        path.write_text("id,when\n1,2024-01-05\n2,2024-02-11\n3,2024-03-20\n",
                        encoding="utf-8")
        profile = dl.analyse(path)
        self.assertEqual([i for i in profile.issues if i.title == "日期格式混用"], [])


class TestCorrelation(unittest.TestCase):
    def test_perfect_positive(self):
        r = dl.pearson([1.0, 2.0, 3.0, 4.0], [2.0, 4.0, 6.0, 8.0])
        self.assertAlmostEqual(r, 1.0, places=6)

    def test_perfect_negative(self):
        r = dl.pearson([1.0, 2.0, 3.0, 4.0], [8.0, 6.0, 4.0, 2.0])
        self.assertAlmostEqual(r, -1.0, places=6)

    def test_constant_series_is_undefined(self):
        self.assertIsNone(dl.pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]))

    def test_too_few_points(self):
        self.assertIsNone(dl.pearson([1.0, 2.0], [1.0, 2.0]))

    def test_correlations_found(self):
        cols = [
            dl.profile_column("a", 0, [str(i) for i in range(50)]),
            dl.profile_column("b", 1, [str(i * 3) for i in range(50)]),
            dl.profile_column("c", 2, [str((i * 7) % 13) for i in range(50)]),
        ]
        pairs = dl.compute_correlations(cols)
        names = {frozenset((a, b)) for a, b, _ in pairs}
        self.assertIn(frozenset(("a", "b")), names)

    def test_text_columns_ignored(self):
        cols = [
            dl.profile_column("a", 0, [str(i) for i in range(50)]),
            dl.profile_column("t", 1, [f"v{i % 7}" for i in range(50)]),
        ]
        self.assertEqual(dl.compute_correlations(cols), [])

    def test_redundant_group_reported_once(self):
        rows = [[str(i), str(i * 2), str(i * 4)] for i in range(60)]
        path = Path(tempfile.mktemp(suffix=".csv"))
        self.addCleanup(path.unlink, missing_ok=True)
        path.write_text("a,b,c\n" + "\n".join(",".join(r) for r in rows) + "\n",
                        encoding="utf-8")
        profile = dl.analyse(path)
        redundant = [i for i in profile.issues if i.title == "疑似冗余列组"]
        self.assertEqual(len(redundant), 1, [i.detail for i in redundant])
        for name in ("a", "b", "c"):
            self.assertIn(f"「{name}」", redundant[0].detail)


class TestCleaning(unittest.TestCase):
    def test_to_halfwidth_digits(self):
        self.assertEqual(dl.to_halfwidth("１２３"), "123")

    def test_to_halfwidth_letters_and_punct(self):
        self.assertEqual(dl.to_halfwidth("ＡＢＣ（ｘ）"), "ABC(x)")

    def test_to_halfwidth_leaves_han_alone(self):
        self.assertEqual(dl.to_halfwidth("中文测试"), "中文测试")

    def test_to_halfwidth_ideographic_space(self):
        self.assertEqual(dl.to_halfwidth("a\u3000b"), "a b")

    def test_strip_control_keeps_tab(self):
        self.assertEqual(dl.strip_control("a\tb\x00c"), "a\tbc")

    def test_clean_trims_whitespace(self):
        header, rows, report = dl.clean_table(["a"], [["  x  "], ["y"]])
        self.assertEqual(rows, [["x"], ["y"]])
        self.assertEqual(report.whitespace, 1)

    def test_clean_converts_fullwidth(self):
        _, rows, report = dl.clean_table(["a"], [["１２３"]])
        self.assertEqual(rows, [["123"]])
        self.assertEqual(report.fullwidth, 1)

    def test_clean_normalises_missing_markers(self):
        _, rows, _ = dl.clean_table(["a"], [["NA"], ["null"], ["keep"]])
        self.assertEqual(rows, [[""], [""], ["keep"]])

    def test_clean_can_keep_missing_markers(self):
        _, rows, _ = dl.clean_table(["a"], [["NA"]], normalise_missing=False)
        self.assertEqual(rows, [["NA"]])

    def test_clean_dedupes_rows(self):
        _, rows, report = dl.clean_table(["a", "b"], [["1", "2"], ["1", "2"], ["3", "4"]],
                                         dedupe=True)
        self.assertEqual(len(rows), 2)
        self.assertEqual(report.rows_dropped, 1)

    def test_clean_drops_empty_columns(self):
        header, rows, report = dl.clean_table(["a", "b", "c"],
                                              [["1", "", "3"], ["4", "", "6"]],
                                              drop_empty_cols=True)
        self.assertEqual(header, ["a", "c"])
        self.assertEqual(rows[0], ["1", "3"])
        self.assertEqual(report.cols_dropped, 1)

    def test_clean_renames_duplicate_headers(self):
        header, _, report = dl.clean_table(["a", "a"], [["1", "2"]])
        self.assertEqual(header, ["a", "a_2"])
        self.assertEqual(report.headers_fixed, 1)

    def test_clean_names_empty_headers(self):
        header, _, _ = dl.clean_table(["a", ""], [["1", "2"]])
        self.assertEqual(header, ["a", "column_2"])

    def test_clean_keeps_headers_when_asked(self):
        header, _, _ = dl.clean_table(["a", "a"], [["1", "2"]], fix_headers=False)
        self.assertEqual(header, ["a", "a"])

    def test_clean_reports_column_count(self):
        _, _, report = dl.clean_table(["a", "b"], [["1", "2"]])
        self.assertEqual(report.cols_written, 2)
        self.assertEqual(report.rows_written, 1)


class TestFixCli(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.source = self.dir / "dirty.csv"
        self.source.write_text(
            "name,amount\n  张三  ,１２３\n李四,NA\n李四,NA\n", encoding="utf-8"
        )

    def test_fix_writes_cleaned_file(self):
        target = self.dir / "out.csv"
        code = dl.main([str(self.source), "--no-color", "--fix", "-o", str(target),
                        "--dedupe"])
        self.assertEqual(code, 0)
        self.assertTrue(target.exists())

    def test_fixed_output_is_actually_cleaner(self):
        target = self.dir / "out.csv"
        dl.main([str(self.source), "--no-color", "--fix", "-o", str(target), "--dedupe"])
        before = dl.analyse(self.source).score
        after = dl.analyse(target).score
        self.assertGreater(after, before)

    def test_default_output_name(self):
        dl.main([str(self.source), "--no-color", "--fix"])
        expected = self.dir / "dirty.cleaned.csv"
        self.assertTrue(expected.exists())

    def test_output_has_bom_for_excel(self):
        target = self.dir / "bom.csv"
        dl.main([str(self.source), "--no-color", "--fix", "-o", str(target)])
        self.assertTrue(target.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_no_bom_option(self):
        target = self.dir / "nobom.csv"
        dl.main([str(self.source), "--no-color", "--fix", "-o", str(target), "--no-bom"])
        self.assertFalse(target.read_bytes().startswith(b"\xef\xbb\xbf"))


class TestCli(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_clean_file_exits_zero(self):
        path = self.dir / "ok.csv"
        path.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
        self.assertEqual(dl.main([str(path), "--no-color"]), 0)

    def test_broken_file_exits_one(self):
        path = self.dir / "bad.csv"
        path.write_text("a,a\n1,2\n", encoding="utf-8")
        self.assertEqual(dl.main([str(path), "--no-color"]), 1)

    def test_missing_file_exits_two(self):
        self.assertEqual(dl.main([str(self.dir / "nope.csv"), "--no-color"]), 2)

    def test_writes_html(self):
        path = self.dir / "ok2.csv"
        path.write_text("a\n1\n", encoding="utf-8")
        target = self.dir / "out.html"
        dl.main([str(path), "--no-color", "-o", str(target)])
        self.assertTrue(target.exists())
        self.assertIn("dataloupe", target.read_text(encoding="utf-8"))

    def test_creates_missing_output_folders(self):
        path = self.dir / "ok3.csv"
        path.write_text("a\n1\n", encoding="utf-8")
        target = self.dir / "reports" / "march" / "out.html"
        self.assertEqual(dl.main([str(path), "--no-color", "-o", str(target)]), 0)
        self.assertTrue(target.exists())

    def test_fix_writes_next_to_the_source_by_default(self):
        path = self.dir / "dirty.csv"
        path.write_text("a,b\n 1 ,2\n", encoding="utf-8")
        self.assertEqual(dl.main([str(path), "--no-color", "--fix"]), 0)
        cleaned = self.dir / "dirty.cleaned.csv"
        self.assertTrue(cleaned.exists())
        self.assertIn("1,2", cleaned.read_text(encoding="utf-8-sig"))


class TestColumnAccumulator(unittest.TestCase):
    def test_matches_one_shot_profile(self):
        values = [str(i % 37) for i in range(500)]
        expected = dl.profile_column("n", 0, values)
        acc = dl.ColumnAccumulator("n", 0)
        for value in values:
            acc.add(value)
        col = acc.finish()
        self.assertEqual(col.kind, expected.kind)
        self.assertEqual(col.distinct, expected.distinct)
        self.assertAlmostEqual(col.num_mean, expected.num_mean, places=9)
        self.assertAlmostEqual(col.num_median, expected.num_median, places=9)
        self.assertAlmostEqual(col.num_std, expected.num_std, places=9)

    def test_welford_matches_the_statistics_module(self):
        import random
        import statistics

        rng = random.Random(3)
        values = [round(rng.gauss(100, 15), 6) for _ in range(5000)]
        acc = dl.ColumnAccumulator("x", 0)
        for value in values:
            acc.add(str(value))
        col = acc.finish()
        self.assertAlmostEqual(col.num_mean, statistics.fmean(values), places=6)
        self.assertAlmostEqual(col.num_std, statistics.pstdev(values), places=6)

    def test_type_settles_on_the_first_sample(self):
        acc = dl.ColumnAccumulator("x", 0)
        for i in range(dl.TYPE_SAMPLE):
            acc.add(str(i))
        for _ in range(50):
            acc.add("oops")
        col = acc.finish()
        # Same rule as before: only the first TYPE_SAMPLE values vote.
        self.assertEqual(col.kind, "int")

    def test_distinct_cap_reports_a_floor(self):
        original = dl.DISTINCT_CAP
        dl.DISTINCT_CAP = 10
        self.addCleanup(setattr, dl, "DISTINCT_CAP", original)

        acc = dl.ColumnAccumulator("t", 0)
        for i in range(100):
            acc.add(f"v{i}")
        col = acc.finish()
        self.assertTrue(col.distinct_capped)
        self.assertEqual(col.distinct, 10)

    def test_reservoir_keeps_order_statistics_close(self):
        original = dl.MAX_VALUES
        dl.MAX_VALUES = 1000
        self.addCleanup(setattr, dl, "MAX_VALUES", original)

        acc = dl.ColumnAccumulator("x", 0)
        for i in range(200_000):
            acc.add(str(i))
        col = acc.finish()
        self.assertTrue(col.approximate)
        self.assertEqual(col.num_min, 0.0)
        self.assertEqual(col.num_max, 199_999.0)
        # Exact statistics must stay exact even when the sample is small.
        self.assertAlmostEqual(col.num_mean, 99_999.5, places=4)
        # Sampled ones only need to be close: within 2% of the true median.
        self.assertAlmostEqual(col.num_median, 99_999.5, delta=4000)

    def test_empty_column_is_empty(self):
        acc = dl.ColumnAccumulator("e", 0)
        for value in ("", "NA", "  ", "null"):
            acc.add(value)
        col = acc.finish()
        self.assertEqual(col.kind, "empty")
        self.assertEqual(col.missing, 4)
        self.assertEqual(col.total, 4)


class TestStreaming(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_source_is_lazy(self):
        path = self.dir / "a.csv"
        path.write_text("a,b\n0,0\n1,2\n", encoding="utf-8")
        stream, rows = dl.iter_source(path)
        self.addCleanup(rows.close)
        self.assertNotIsInstance(rows, list)
        self.assertEqual(next(rows), ["0", "0"])
        self.assertEqual(stream.stats.read, 1)

    def test_streamed_profile_matches_the_in_memory_one(self):
        path = self.dir / "b.csv"
        lines = ["x,y"] + [f"{i},{'a' if i % 3 else ''}" for i in range(300)]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        streamed = dl.analyse(path)
        loaded = dl.load_source(path)
        for idx, col in enumerate(streamed.columns):
            values = [row[idx] for row in loaded.rows]
            expected = dl.profile_column(col.name, idx, values)
            self.assertEqual(col.total, expected.total)
            self.assertEqual(col.missing, expected.missing)
            self.assertEqual(col.kind, expected.kind)
            self.assertEqual(col.distinct, expected.distinct)

    def test_a_very_long_field_is_not_rejected(self):
        # csv's default 128 KiB field cap would otherwise raise here.
        path = self.dir / "long.csv"
        path.write_text("a,b\n" + "x" * 300_000 + ",1\n", encoding="utf-8")
        profile = dl.analyse(path)
        self.assertEqual(profile.rows, 1)
        self.assertEqual(profile.columns[0].max_len, 300_000)

    def test_ragged_rows_are_padded_to_header_width(self):
        path = self.dir / "r.csv"
        path.write_text("a,b,c\n1,2\n1,2,3,4\n", encoding="utf-8")
        profile = dl.analyse(path)
        self.assertEqual(profile.ragged_rows, 2)
        self.assertEqual(profile.rows, 2)
        self.assertEqual(profile.columns[2].total, 2)

    def test_duplicate_rows_are_counted(self):
        path = self.dir / "d.csv"
        path.write_text("a,b\n1,2\n1,2\n3,4\n", encoding="utf-8")
        self.assertEqual(dl.analyse(path).duplicate_rows, 1)

    def test_limit_stops_after_n_rows(self):
        path = self.dir / "l.csv"
        path.write_text("a\n" + "".join(f"{i}\n" for i in range(50)), encoding="utf-8")
        profile = dl.analyse(path, limit=10)
        self.assertEqual(profile.rows, 10)
        self.assertTrue(profile.truncated)

    def test_blank_lines_are_skipped_not_counted(self):
        path = self.dir / "blank.csv"
        path.write_text("a\n1\n\n\n2\n", encoding="utf-8")
        profile = dl.analyse(path)
        self.assertEqual(profile.rows, 2)
        self.assertEqual(profile.empty_rows, 2)

    def test_pearson_drops_whole_pairs(self):
        import math

        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [2.0, math.nan, 6.0, 8.0, 10.0]
        self.assertAlmostEqual(dl.pearson(xs, ys), 1.0, places=9)

    def test_correlations_compare_the_same_rows(self):
        # A wave, not a straight line: if the two columns were sampled
        # independently their rows would drift apart and r would collapse.
        import math

        path = self.dir / "wave.csv"
        lines = ["x,y"]
        for i in range(400):
            y = "" if i % 7 == 0 else f"{math.sin(i / 5):.6f}"
            lines.append(f"{i},{y}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        profile = dl.analyse(path)
        samples = [c.values_sample for c in profile.columns if c.kind in ("int", "float")]
        self.assertEqual(len({len(s) for s in samples}), 1, "samples are not row-aligned")

        # x is a ramp and y is a wave, so this is not a strong pair; what
        # matters is that every pair comes from one row, which the alignment
        # assertion above pins down. Check the plumbing survives gaps at all.
        self.assertEqual(profile.columns[1].missing, 58)

    def test_strong_correlation_survives_gaps(self):
        path = self.dir / "gaps.csv"
        lines = ["price,price_cny"]
        for i in range(300):
            cny = "" if i % 5 == 0 else str(i * 7 + 3)
            lines.append(f"{i},{cny}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        profile = dl.analyse(path)
        self.assertTrue(profile.correlations)
        a, b, r = profile.correlations[0]
        self.assertEqual({a, b}, {"price", "price_cny"})
        self.assertGreater(r, 0.99)


class TestMemoryCeiling(unittest.TestCase):
    """The point of streaming: memory must not follow the row count.

    Measured against ``load_source`` on the same file rather than against a
    hard-coded megabyte figure, so the test means the same thing on every
    machine and in every Python version.
    """

    ROWS = 200_000

    def test_streaming_uses_less_memory_than_loading(self):
        import tracemalloc

        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        path = directory / "big.csv"

        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write("id,amount,region,when\n")
            for i in range(self.ROWS):
                handle.write(f"{i},{i % 997},R{i % 13},2024-{(i % 12) + 1:02d}-01\n")

        tracemalloc.start()
        profile = dl.analyse(path)
        _, streamed = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        tracemalloc.start()
        dl.load_source(path)
        _, loaded = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        self.assertEqual(profile.rows, self.ROWS)
        self.assertLess(
            streamed / loaded, 0.8,
            f"streaming used {streamed / 1048576:.1f} MB, "
            f"loading used {loaded / 1048576:.1f} MB — streaming should be well under",
        )
        for col in profile.columns:
            self.assertLessEqual(len(col.values_sample), dl.CORRELATION_SAMPLE)
            self.assertLessEqual(col.distinct, dl.DISTINCT_CAP)


class TestFormulaInjection(unittest.TestCase):
    def test_flags_the_known_payloads(self):
        for payload in ("=cmd|'/C calc'!A0", "@SUM(A1:A9)", '=HYPERLINK("http://x")',
                        "+cmd|'/C calc'!A0", "-2+3+cmd|'/C calc'!A0"):
            self.assertTrue(dl.looks_like_formula(payload), payload)

    def test_leaves_plain_numbers_alone(self):
        for value in ("-5", "+3", "-1.5e3", ".5", "2024-01-01", "abc"):
            self.assertFalse(dl.looks_like_formula(value), value)

    def test_reported_as_a_medium_issue(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        path = directory / "inject.csv"
        path.write_text(
            "name,note\n"
            "a,=cmd|'/C calc'!A0\n"
            "b,hello\n",
            encoding="utf-8",
        )
        profile = dl.analyse(path)
        titles = [issue.title for issue in profile.issues if issue.column == "note"]
        self.assertIn("存在可被 Excel 执行的单元格", titles)
        self.assertEqual(profile.columns[1].formula_cells, 1)


class TestPii(unittest.TestCase):
    """Sensitive-pattern detection has to be precise: a stray email in a name
    column must not fire, but a column that *is* emails must."""

    def test_each_pattern_matches_its_own_kind(self):
        cases = {
            "邮箱": "user.name+tag@example.co.uk",
            "身份证号": "110101199003071234",
            "手机号": "13800138000",
            "IPv4 地址": "192.168.1.1",
            "URL": "https://example.com/path",
        }
        for label, value in cases.items():
            scored = dl.detect_pii([value] * 10)
            self.assertIn(label, scored, value)
            self.assertEqual(scored[label], 1.0)

    def test_id_card_is_not_double_flagged_as_bank_card(self):
        scored = dl.detect_pii(["110101199003071234"] * 10)
        self.assertIn("身份证号", scored)
        self.assertNotIn("银行卡号", scored)

    def test_bank_card_passes_luhn_only(self):
        # A real Luhn-valid 16-digit card number.
        self.assertIn("银行卡号", dl.detect_pii(["4111 1111 1111 1111"] * 10))
        # A random 16-digit string that fails Luhn is not a card.
        self.assertNotIn("银行卡号", dl.detect_pii(["1234 5678 9012 3456"] * 10))

    def test_a_single_stray_email_does_not_flag_the_column(self):
        # 9 names, 1 email: the email share is 10%, under the 80% bar.
        scored = dl.detect_pii(["张三", "李四", "王五", "赵六", "钱七",
                                "孙八", "周九", "吴十", "郑一", "a@b.com"])
        self.assertNotIn("邮箱", scored)

    def test_reported_as_a_high_severity_issue(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        path = directory / "pii.csv"
        path.write_text(
            "id,email\n" + "".join(f"{i},user{i}@example.com\n" for i in range(12)),
            encoding="utf-8",
        )
        profile = dl.analyse(path)
        email = profile.columns[1]
        self.assertIn("邮箱", email.pii)
        titles = [issue.title for issue in profile.issues if issue.column == "email"]
        self.assertIn("疑似包含敏感信息", titles)

    def test_phone_in_an_int_column_is_still_found(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        # No header/quote tricks: bare 11-digit numbers land in an int column.
        path = directory / "phones.csv"
        path.write_text(
            "phone\n" + "".join(f"138{i:08d}\n" for i in range(12)),
            encoding="utf-8",
        )
        profile = dl.analyse(path)
        phone = profile.columns[0]
        self.assertIn("手机号", phone.pii)


class TestHeaderCollisions(unittest.TestCase):
    def test_case_only_collision_is_reported(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        path = directory / "case.csv"
        path.write_text("id,ID,name\n1,2,x\n", encoding="utf-8")

        profile = dl.analyse(path)
        titles = [issue.title for issue in profile.issues]
        self.assertIn("列名仅大小写不同", titles)
        # They are distinct names, so the exact-duplicate check must not fire.
        self.assertNotIn("列名重复", titles)

    def test_identical_names_still_use_the_high_severity_path(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        path = directory / "dup.csv"
        path.write_text("a,a\n1,2\n", encoding="utf-8")

        profile = dl.analyse(path)
        titles = [issue.title for issue in profile.issues]
        self.assertIn("列名重复", titles)
        self.assertNotIn("列名仅大小写不同", titles)


class TestSqlOutput(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)

    def _profile(self, text: str, name: str = "t.csv"):
        path = self.directory / name
        path.write_text(text, encoding="utf-8")
        return dl.analyse(path)

    def test_maps_types_for_postgres(self):
        profile = self._profile("a,b,c,d\n1,1.5,true,2024-01-01\n2,2.5,false,2024-02-01\n")
        sql = dl.render_sql(profile, "postgres")
        self.assertIn("CREATE TABLE t (", sql)
        self.assertIn("a  BIGINT", sql)
        self.assertIn("b  DOUBLE PRECISION", sql)
        self.assertIn("c  BOOLEAN", sql)
        self.assertIn("d  DATE", sql)

    def test_sqlite_has_no_date_or_boolean_types(self):
        profile = self._profile("a,b\n1,2024-01-01\n2,2024-02-01\n")
        sql = dl.render_sql(profile, "sqlite")
        self.assertIn("a  INTEGER", sql)
        self.assertIn("b  TEXT", sql)

    def test_time_component_promotes_date_to_timestamp(self):
        profile = self._profile("t\n2024-01-01 09:30:00\n2024-01-02 10:00:00\n")
        self.assertIn("TIMESTAMP", dl.render_sql(profile, "postgres"))

    def test_awkward_names_are_quoted(self):
        profile = self._profile("Order,我的列\n1,2\n")
        sql = dl.render_sql(profile, "postgres")
        self.assertIn('"Order"', sql)
        self.assertIn('"我的列"', sql)

    def test_mysql_quotes_with_backticks(self):
        profile = self._profile("Order\n1\n")
        self.assertIn("`Order`", dl.render_sql(profile, "mysql"))

    def test_table_name_can_be_overridden(self):
        profile = self._profile("a\n1\n")
        self.assertIn("CREATE TABLE events (", dl.render_sql(profile, "postgres", "events"))

    def test_unknown_dialect_is_rejected(self):
        profile = self._profile("a\n1\n")
        with self.assertRaises(ValueError):
            dl.render_sql(profile, "oracle")

    def test_cli_prints_sql_and_exits_zero(self):
        path = self.directory / "ok.csv"
        path.write_text("a,a\n1,2\n", encoding="utf-8")
        self.assertEqual(dl.main([str(path), "--no-color", "--sql", "postgres"]), 0)

    def test_json_honours_the_exit_code(self):
        # The README advertises --json as a CI gate, so it has to fail the build.
        bad = self.directory / "bad.csv"
        bad.write_text("a,a\n1,2\n", encoding="utf-8")
        self.assertEqual(dl.main([str(bad), "--no-color", "--json"]), 1)

    def test_fail_under_gates_on_the_score(self):
        path = self.directory / "ok.csv"
        path.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
        self.assertEqual(dl.main([str(path), "--no-color", "--fail-under", "50"]), 0)
        # The threshold is inclusive: a score of exactly 100 passes a bar of 100.
        self.assertEqual(dl.main([str(path), "--no-color", "--fail-under", "100"]), 0)
        self.assertEqual(dl.main([str(path), "--no-color", "--fail-under", "101"]), 1)


class TestEdgeCases(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)

    def test_header_only_file_is_flagged(self):
        path = self.directory / "header.csv"
        path.write_text("a,b,c\n", encoding="utf-8")
        profile = dl.analyse(path)
        self.assertEqual(profile.rows, 0)
        titles = [issue.title for issue in profile.issues]
        self.assertIn("没有数据行", titles)
        self.assertLess(profile.score, 100)

    def test_flat_json_array_becomes_one_column(self):
        path = self.directory / "flat.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        profile = dl.analyse(path)
        self.assertEqual(profile.rows, 3)
        self.assertEqual(profile.header, ["value"])
        self.assertEqual(profile.columns[0].kind, "int")

    def test_fix_refuses_to_overwrite_the_source(self):
        path = self.directory / "data.csv"
        path.write_text("a,b\n 1 ,2\n", encoding="utf-8")
        before = path.read_bytes()
        self.assertEqual(dl.main([str(path), "--no-color", "--fix", "-o", str(path)]), 2)
        self.assertEqual(path.read_bytes(), before)

    def test_single_column_file(self):
        path = self.directory / "one.csv"
        path.write_text("a\n1\n2\n3\n", encoding="utf-8")
        profile = dl.analyse(path)
        self.assertEqual((profile.rows, profile.cols), (3, 1))

    def test_quoted_delimiter_inside_a_field(self):
        path = self.directory / "q.csv"
        path.write_text('a,b\n"x,y",2\n', encoding="utf-8")
        profile = dl.analyse(path)
        self.assertEqual(profile.cols, 2)
        self.assertEqual(profile.preview[0][0], "x,y")

    def test_empty_file_is_an_error_not_a_crash(self):
        path = self.directory / "empty.csv"
        path.write_bytes(b"")
        with self.assertRaises(ValueError):
            dl.analyse(path)


class TestDiff(unittest.TestCase):
    """Comparing two exports of the same logical table is its own feature."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)

    def test_same_file_has_no_drift(self):
        path = self.directory / "same.csv"
        path.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
        diff = dl.diff_profiles(dl.analyse(path), dl.analyse(path))
        self.assertFalse(diff.drift)
        self.assertFalse(diff.breaking)

    def test_detects_added_and_removed_columns(self):
        old = self.directory / "old.csv"
        new = self.directory / "new.csv"
        old.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
        new.write_text("a,b,d\n1,2,4\n", encoding="utf-8")
        diff = dl.diff_profiles(dl.analyse(old), dl.analyse(new))
        self.assertEqual(diff.removed, ["c"])
        self.assertEqual(diff.added, ["d"])
        self.assertTrue(diff.drift)

    def test_renames_are_guessed_from_name_and_type(self):
        # price -> unit_price keeps the type and contains the old name.
        old = self.directory / "old.csv"
        new = self.directory / "new.csv"
        old.write_text("id,price\n1,10.0\n", encoding="utf-8")
        new.write_text("id,unit_price\n1,10.0\n", encoding="utf-8")
        diff = dl.diff_profiles(dl.analyse(old), dl.analyse(new))
        self.assertEqual(diff.renamed, [("price", "unit_price")])
        self.assertEqual(diff.added, [])
        self.assertEqual(diff.removed, [])

    def test_unrelated_drop_and_add_are_not_a_rename(self):
        old = self.directory / "old.csv"
        new = self.directory / "new.csv"
        old.write_text("id,alpha\n1,10\n", encoding="utf-8")
        new.write_text("id,beta\n1,10\n", encoding="utf-8")
        diff = dl.diff_profiles(dl.analyse(old), dl.analyse(new))
        self.assertEqual(diff.renamed, [])
        self.assertEqual(diff.removed, ["alpha"])
        self.assertEqual(diff.added, ["beta"])

    def test_breaking_type_change_is_flagged(self):
        old = self.directory / "old.csv"
        new = self.directory / "new.csv"
        # n is clean int in the old file; a "TBD" appears in the new one, so the
        # inferred kind stays int (plurality) but the mismatch rate jumps -- the
        # quiet corruption a drift check exists to catch.
        old.write_text("a,n\n1,5\n2,6\n", encoding="utf-8")
        new.write_text("a,n\n1,5\n2,TBD\n", encoding="utf-8")
        diff = dl.diff_profiles(dl.analyse(old), dl.analyse(new))
        names = [c[0] for c in diff.type_inconsistencies]
        self.assertIn("n", names)
        self.assertTrue(diff.breaking)

    def test_quality_regression_on_missing_rate(self):
        old = self.directory / "old.csv"
        new = self.directory / "new.csv"
        old.write_text("a,b\n1,x\n2,y\n3,z\n", encoding="utf-8")
        new.write_text("a,b\n1,x\n2,\n3,\n", encoding="utf-8")
        diff = dl.diff_profiles(dl.analyse(old), dl.analyse(new))
        names = [c[0] for c in diff.quality_regressions]
        self.assertIn("b", names)
        self.assertTrue(diff.breaking)

    def test_non_breaking_drift_exits_zero_unless_strict(self):
        old = self.directory / "old.csv"
        new = self.directory / "new.csv"
        old.write_text("a,b\n1,2\n", encoding="utf-8")
        new.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
        old_p, new_p = dl.analyse(old), dl.analyse(new)
        # Only an added column: a warning, not a break.
        self.assertFalse(dl.diff_profiles(old_p, new_p).breaking)
        self.assertEqual(dl.main([str(old), str(new), "--no-color", "--diff"]), 0)
        self.assertEqual(
            dl.main([str(old), str(new), "--no-color", "--diff", "--strict"]), 1)

    def test_diff_reports_drift_in_cli(self):
        old = self.directory / "old.csv"
        new = self.directory / "new.csv"
        old.write_text("a,b\n1,2\n", encoding="utf-8")
        new.write_text("b\n2\n", encoding="utf-8")
        # column a removed -> breaking
        self.assertEqual(dl.main([str(old), str(new), "--no-color", "--diff"]), 1)

    def test_edit_distance(self):
        self.assertEqual(dl._edit_distance("kitten", "sitting"), 3)
        self.assertEqual(dl._edit_distance("price", "unit_price"), 5)
        self.assertEqual(dl._edit_distance("abc", "abc"), 0)
        self.assertEqual(dl._edit_distance("", "ab"), 2)


class TestDistributionShift(unittest.TestCase):
    """A column whose centre moved is a change even when its type did not."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)

    def _diff(self, old_body: str, new_body: str, header: str = "amount"):
        old = self.directory / "old.csv"
        new = self.directory / "new.csv"
        old.write_text(f"{header}\n{old_body}", encoding="utf-8")
        new.write_text(f"{header}\n{new_body}", encoding="utf-8")
        return dl.diff_profiles(dl.analyse(old), dl.analyse(new))

    def test_a_large_mean_move_is_reported(self):
        # Deliberately non-consecutive: a run of 40 consecutive integers is an
        # auto-increment key, which the sequence guard skips.
        old = "".join(f"{100 + i % 7}\n" for i in range(40))
        new = "".join(f"{400 + i % 7}\n" for i in range(40))
        diff = self._diff(old, new)
        shifts = {s.column: s for s in diff.distribution_shifts}
        self.assertIn("amount", shifts)
        self.assertGreater(shifts["amount"].relative, 2.0)
        self.assertTrue(diff.drift)

    def test_a_stable_column_is_not_reported(self):
        old = "".join(f"{100 + i % 5}\n" for i in range(40))
        new = "".join(f"{100 + i % 5}\n" for i in range(40))
        diff = self._diff(old, new)
        self.assertEqual(diff.distribution_shifts, [])
        self.assertFalse(diff.drift)

    def test_an_auto_increment_column_is_ignored(self):
        # Same shape, ids shifted by 1000: the mean moves by many sigmas, but
        # an id column's mean is a row counter, not a measurement.
        old = "".join(f"{i}\n" for i in range(50))
        new = "".join(f"{i + 1000}\n" for i in range(50))
        diff = self._diff(old, new, header="order_id")
        self.assertEqual(diff.distribution_shifts, [])

    def test_a_distribution_shift_is_not_breaking(self):
        old = "".join(f"{100 + i % 7}\n" for i in range(40))
        new = "".join(f"{400 + i % 7}\n" for i in range(40))
        diff = self._diff(old, new)
        self.assertTrue(diff.drift)
        self.assertFalse(diff.breaking)

    def test_json_carries_the_shift(self):
        old = self.directory / "old.csv"
        new = self.directory / "new.csv"
        old.write_text("amount\n" + "".join(f"{100 + i % 7}\n" for i in range(40)), encoding="utf-8")
        new.write_text("amount\n" + "".join(f"{400 + i % 7}\n" for i in range(40)), encoding="utf-8")
        diff = dl.diff_profiles(dl.analyse(old), dl.analyse(new))
        payload = json.loads(dl.render_diff_json(diff))
        self.assertEqual(payload["distribution_shifts"][0]["column"], "amount")


class TestRowDiff(unittest.TestCase):
    """Row-level comparison answers "did rows disappear" without holding the
    files in memory."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)

    def test_overlap_and_disappearance(self):
        old = self.directory / "old.csv"
        new = self.directory / "new.csv"
        old.write_text("id,v\n1,a\n2,b\n3,c\n", encoding="utf-8")
        new.write_text("id,v\n2,b\n3,c\n4,d\n", encoding="utf-8")
        diff = dl.compare_rows(old, new, ["id", "v"])
        self.assertEqual(diff.common, 2)
        self.assertEqual(diff.old_only, 1)
        self.assertEqual(diff.new_only, 1)
        self.assertTrue(diff.exact)

    def test_identical_files_have_no_difference(self):
        path = self.directory / "same.csv"
        path.write_text("id,v\n1,a\n2,b\n", encoding="utf-8")
        diff = dl.compare_rows(path, path, ["id", "v"])
        self.assertEqual((diff.old_only, diff.new_only, diff.common), (0, 0, 2))

    def test_only_shared_columns_take_part(self):
        # The new file gained a column; comparing whole rows would call every
        # row different. Comparing on the shared columns must not.
        old = self.directory / "old.csv"
        new = self.directory / "new.csv"
        old.write_text("id,v\n1,a\n2,b\n", encoding="utf-8")
        new.write_text("id,v,extra\n1,a,x\n2,b,y\n", encoding="utf-8")
        diff = dl.compare_rows(old, new, ["id", "v"])
        self.assertEqual(diff.common, 2)
        self.assertEqual(diff.old_only, 0)
        self.assertEqual(diff.new_only, 0)

    def test_duplicate_rows_collapse_to_one_distinct_row(self):
        old = self.directory / "old.csv"
        new = self.directory / "new.csv"
        old.write_text("id\n1\n1\n1\n", encoding="utf-8")
        new.write_text("id\n1\n", encoding="utf-8")
        diff = dl.compare_rows(old, new, ["id"])
        self.assertEqual(diff.common, 1)
        self.assertEqual(diff.old_only, 0)

    def test_lost_rows_count_as_drift(self):
        old = self.directory / "old.csv"
        new = self.directory / "new.csv"
        old.write_text("id,v\n1,a\n2,b\n3,c\n", encoding="utf-8")
        new.write_text("id,v\n1,a\n", encoding="utf-8")
        # Data loss is drift, not a schema break: a rolling-window export drops
        # rows by design, so only --strict turns it into a build failure.
        self.assertEqual(dl.main([str(old), str(new), "--diff", "--no-color"]), 0)
        self.assertEqual(dl.main([str(old), str(new), "--diff", "--no-color", "--strict"]), 1)

    def test_no_rows_skips_the_comparison(self):
        old = self.directory / "old.csv"
        new = self.directory / "new.csv"
        old.write_text("id\n1\n", encoding="utf-8")
        new.write_text("id\n1\n2\n", encoding="utf-8")
        self.assertEqual(dl.main([str(old), str(new), "--diff", "--no-color", "--no-rows"]), 0)


class TestMask(unittest.TestCase):
    """Masking has to destroy the identifier while leaving the value readable."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)

    def test_email_keeps_the_domain(self):
        masked, label = dl.mask_cell("zhangwei@example.com")
        self.assertEqual(label, "邮箱")
        self.assertEqual(masked, "z***@example.com")

    def test_phone_keeps_the_last_four(self):
        masked, label = dl.mask_cell("13800138000")
        self.assertEqual(label, "手机号")
        self.assertEqual(masked, "138****8000")

    def test_id_card_keeps_the_ends(self):
        masked, label = dl.mask_cell("110101199003071234")
        self.assertEqual(label, "身份证号")
        self.assertEqual(masked, "110101********1234")

    def test_bank_card_with_separators_is_masked(self):
        masked, label = dl.mask_cell("4111 1111 1111 1111")
        self.assertEqual(label, "银行卡号")
        self.assertEqual(masked, "************1111")

    def test_bare_digits_are_not_a_card_by_default(self):
        # A 16-digit order number must survive unless the column was flagged.
        self.assertIsNone(dl.mask_cell("4111111111111111"))
        self.assertIsNotNone(dl.mask_cell("4111111111111111", allow_bank_card=True))

    def test_ordinary_values_pass_through(self):
        for value in ("张伟", "42", "2024-01-05", "", "hello world"):
            self.assertIsNone(dl.mask_cell(value), value)

    def test_masked_output_no_longer_looks_sensitive(self):
        # The whole point: a masked file must not itself trip the detector.
        path = self.directory / "pii.csv"
        path.write_text(
            "email,phone\n"
            + "".join(f"user{i}@example.com,138{i:08d}\n" for i in range(12)),
            encoding="utf-8",
        )
        profile = dl.analyse(path)
        flagged = {c.name: set(c.pii) for c in profile.columns if c.pii}
        source = dl.load_source(path)
        _, rows, report = dl.mask_table(source.header, source.rows, flagged)
        self.assertEqual(report.cells_masked, 24)
        self.assertEqual(dl.detect_pii([r[0] for r in rows]), {})
        self.assertEqual(dl.detect_pii([r[1] for r in rows]), {})

    def test_cli_writes_a_masked_copy_and_keeps_the_source(self):
        path = self.directory / "pii.csv"
        path.write_text(
            "email\n" + "".join(f"user{i}@example.com\n" for i in range(12)),
            encoding="utf-8",
        )
        before = path.read_text(encoding="utf-8")
        target = self.directory / "masked.csv"
        self.assertEqual(dl.main([str(path), "--mask", "-o", str(target), "--no-color"]), 0)
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        written = target.read_text(encoding="utf-8-sig")
        self.assertIn("u***@example.com", written)
        self.assertNotIn("user0@example.com", written)

    def test_default_output_name(self):
        path = self.directory / "people.csv"
        path.write_text(
            "email\n" + "".join(f"user{i}@example.com\n" for i in range(12)),
            encoding="utf-8",
        )
        self.assertEqual(dl.main([str(path), "--mask", "--no-color"]), 0)
        self.assertTrue((self.directory / "people.masked.csv").exists())

    def test_refuses_to_overwrite_the_source(self):
        path = self.directory / "people.csv"
        path.write_text("email\n" + "".join(f"u{i}@x.com\n" for i in range(12)), encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        self.assertEqual(dl.main([str(path), "--mask", "-o", str(path), "--no-color"]), 2)
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_mask_all_catches_bare_card_numbers(self):
        path = self.directory / "cards.csv"
        path.write_text(
            "card\n" + "4111111111111111\n" * 12,
            encoding="utf-8",
        )
        target = self.directory / "out.csv"
        self.assertEqual(
            dl.main([str(path), "--mask", "--mask-all", "-o", str(target), "--no-color"]), 0
        )
        self.assertIn("************1111", target.read_text(encoding="utf-8-sig"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
