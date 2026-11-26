"""Unit tests of the rules, and a complete pipeline run on a small temporary repository.

These tests use made-up data and do not read the upstream repositories in data/repos/. To check the real data, use
`python -m fixprop check`.
"""

import json
import subprocess

import pytest

from fixprop import census, gitio, hunks, pairs, paths
from fixprop import normalize as N
from fixprop.codebases import Codebase, SrcFile, rust_crate_dir

# ------------------------------------------------------------------ normalizing and counted lines


def test_whitespace_is_normalized():
    assert N.norm_ws("  a\t =  b ;\r") == "a = b ;"


def test_valid_rust_change_lines():
    assert N.valid_rust_change("timer_f / arr / (psc + 1)")
    assert not N.valid_rust_change("let x = 1;")  # shorter than 16 characters
    assert N.valid_rust_change("x" * 16) and not N.valid_rust_change("x" * 15)
    for s in ("// a comment that is long enough", "#[cfg(feature = \"something\")]", "use crate::pac::timer::vals;"):
        assert not N.valid_rust_change(s)


def test_valid_c_change_lines():
    assert N.valid_c_change("regval |= QSPI_CR_DMAEN;")
    assert not N.valid_c_change("regval |= 1;")  # shorter than 18 characters
    assert not N.valid_c_change("a = b + c + d + e + f;")  # no 4 consecutive letters or underscores
    for s in ("/* comment that is long enough */", "* continued comment line here", "// line comment long enough",
              "#include <nuttx/config.h>", "#endif /* CONFIG_STM32_QSPI */", "#else /* something long */"):
        assert not N.valid_c_change(s)


def test_valid_lines_for_finding_copies():
    assert N.valid_rust_pair("x" * 28) and not N.valid_rust_pair("x" * 27)
    assert not N.valid_rust_pair("pub use crate::something::very::long::path;")
    assert N.valid_rust_change("pub use crate::something::very::long::path;")  # pub use counts when comparing hunks
    assert N.valid_c_pair("static int up_setup(struct uart_dev_s *dev);")
    assert not N.valid_c_pair("#define DT_DRV_COMPAT st_stm32_i2c_v2")


def test_driver_name_prefix_is_replaced():
    norm = N.prefix_normalizer(["stm32", "nrf52"], ["sam"])
    for name in ("stm32l4_i2c.c", "stm32_i2c.c", "STM32F7_i2c.c"):
        assert norm(name) == "P_i2c.c"
    assert norm("  regval  = getreg32(STM32L4_RCC_CR);") == "regval = getreg32(P_RCC_CR);"
    assert norm("nrf52_gpio.c") == "P_gpio.c" and norm("NRF52832_GPIO_BASE") == "P_GPIO_BASE"
    assert norm("sam_serial.c") == "P_serial.c"
    assert norm("sample_rate = 1;") == "sample_rate = 1;"  # names made of letters only match as a whole prefix
    assert N.stem_of("stm32l4") == "stm32" and N.stem_of("gd32f4xx") == "gd32" and N.stem_of("sam") == "sam"


def test_normalizer_does_not_depend_on_input_order():
    a = N.prefix_normalizer(["gd32", "at32", "stm32"], ["sam", "xmc"])
    b = N.prefix_normalizer(["stm32", "at32", "gd32"], ["xmc", "sam"])
    for line in ("at32_gpio_write(GD32F4_BASE, STM32L4_RCC);", "sam_serial_init(); xmc_clock();"):
        assert a(line) == b(line)
    assert N.token_normalizer(["abc", "abd", "longer"])("ABC abd Longer") == "P P P"


def test_zephyr_file_name_tokens():
    assert N.name_tokens("i2c_ll_stm32_v1.c", "i2c") == ["stm32"]
    assert N.name_tokens("clock_control_stm32_ll_h7.c", "clock_control") == ["stm32"]
    assert N.name_tokens("lsm6dso_trigger.c", "sensor") == ["lsm6dso", "trigger"]
    norm = N.token_normalizer(["lsm6dso", "trigger"])
    assert norm("static int LSM6DSO_trigger_set(const struct device *dev)") == "static int P_P_set(const struct device *dev)"


def test_rust_scope_is_crate_src():
    assert rust_crate_dir("embassy-stm32/src/i2c/v1.rs") == "embassy-stm32"
    assert rust_crate_dir("src/lib.rs") == "" and rust_crate_dir("build.rs") is None


def test_copied_from_note_in_file_header():
    text = "//! I2C\n\n// The following code is modified from embassy-stm32\n// https://github.com/x\nuse core::fmt;\n"
    assert pairs.derived_note(text, "rust") == "modified from embassy-stm32"
    assert pairs.derived_note("use a::b;\n// based on something\n", "rust") == ""  # only the file header counts


def test_strip_comments():
    code = N.code_only(N.norm_ws)
    assert code("  x = 1; /* set */ y = 2; // done") == "x = 1; y = 2;"
    assert code("/* only a comment */") == "" and code("/// rust doc comment") == ""
    assert code('url = "http://a/b"; // c') == 'url = "http://a/b";'


def test_fix_keyword_does_not_match_prefix():
    assert not census.FIX.search("standardize public API/type prefix to stm32_ (suffix too)")
    for msg in ("Fix typo", "this fixes #12", "bugfix for uart", "hotfix", "serial: fixed overrun", "re-fix it"):
        assert census.FIX.search(msg), msg


# ------------------------------------------------------------------ judging one hunk

CODE = N.code_only(N.norm_ws)


def judge(diff, target_lines, strict=False, valid=N.valid_c_change):
    view = hunks.make_view([CODE(s) for s in target_lines], valid)
    return [hunks.classify(h, view, CODE, valid, strict) for h in hunks.parse_diff(diff)]


def test_parse_diff():
    d = ("diff --git a/x b/y\n--- a/x\n+++ b/y\n@@ -10,4 +10,4 @@ fn frequency()\n     let psc = regs.psc().read().psc();\n"
         "-    timer_f / arr / (psc + 1)\n+    timer_f / (arr + 1) / (psc + 1)\n }\n@@ -40,2 +40,3 @@ fn start()\n"
         "     regs.sr().write(|w| w.set_uif(false));\n+    regs.egr().write(|w| w.set_ug(true));\n }\n")
    h1, h2 = hunks.parse_diff(d)
    assert (h1.old_start, h1.new_start, h1.dels, h1.adds) == (10, 10, ["    timer_f / arr / (psc + 1)"], ["    timer_f / (arr + 1) / (psc + 1)"])
    assert h2.dels == [] and h2.adds == ["    regs.egr().write(|w| w.set_ug(true));"]
    assert hunks.line_numbers(["a", "x", "b", "x"], ["x"]) == [2, 4]


REPL = ("@@ -10,5 +10,5 @@\n   priv = dev->priv;\n   flags = enter_critical_section();\n"
        "-  if (qh->epinfo == epinfo)\n+  if (qh->epinfo != epinfo)\n   leave_critical_section(flags);\n   return OK;\n")
BEFORE = ["flags = enter_critical_section();", "if (qh->epinfo == epinfo)", "leave_critical_section(flags);"]
AFTER = ["flags = enter_critical_section();", "if (qh->epinfo != epinfo)", "leave_critical_section(flags);"]


def test_replace_candidate_has_fix_and_diverged():
    (a,) = judge(REPL, BEFORE)  # the copy still has the pre-fix code
    assert (a.status, a.kind, a.dels, a.adds) == ("candidate", "replace", ["if (qh->epinfo == epinfo)"], ["if (qh->epinfo != epinfo)"])
    (b,) = judge(REPL, AFTER)  # the copy already has the fixed code
    assert b.status == "has_fix"
    (c,) = judge(REPL, BEFORE + ["if (qh->epinfo != epinfo)"])  # both the old and the new line: not a candidate
    assert c.status == "has_fix"
    (d,) = judge(REPL, ["something_else_entirely_here();"])
    assert (d.status, d.why) == ("diverged", "")


def test_replace_needs_the_same_neighbour_in_the_other_file():
    elsewhere = ["something_unrelated_before();", "if (qh->epinfo == epinfo)", "something_unrelated_after();"]
    (b,) = judge(REPL, elsewhere)
    assert (b.status, b.why) == ("diverged", "unanchored")  # the text is there, but not at the same position


def test_strict_replace_and_delete_only():
    d = ("@@ -1,4 +1,3 @@\n   regval = getreg32(BASE_ADDRESS);\n-  regval |= QSPI_CR_DMAEN;\n   putreg32(regval, BASE_ADDRESS);\n"
         "@@ -9,3 +8,3 @@\n   lock_the_device(dev);\n-  ret = up_setup(dev);\n+  ret = up_setup_locked(dev);\n")
    tgt = ["regval = getreg32(BASE_ADDRESS);", "regval |= QSPI_CR_DMAEN;", "putreg32(regval, BASE_ADDRESS);",
           "lock_the_device(dev);", "ret = up_setup(dev);"]
    dele, repl = judge(d, tgt, strict=True)
    assert (dele.status, dele.kind) == ("candidate", "delete")  # delete-only: a kind of its own
    assert repl.status == "diverged"  # strict: only one counted removed line, shorter than 24 characters
    assert judge(d, tgt, strict=False)[1].status == "candidate"


def test_comment_only_and_reformat_only_changes_are_skipped():
    comment = ("@@ -1,3 +1,3 @@\n   int state;  /* Current state of the driver */\n"
               "-  enum intstate_e event;   /* Last event that occurred with this status */\n"
               "+  enum intstate_e event;   /* Last event that occurred with status */\n   uint32_t status;\n")
    reflow = ("@@ -1,4 +1,3 @@\n-struct imxrt_can flexcans[] =\n-{\n+struct imxrt_can flexcans[] = {\n"
              "   {.name = \"can1\"},\n")
    moved = ("@@ -1,4 +1,4 @@\n-  regval |= QSPI_CR_ENABLE_BIT;\n   putreg32(regval, BASE_ADDRESS);\n"
             "+  regval |= QSPI_CR_ENABLE_BIT;\n")
    short = "@@ -1,3 +1,3 @@\n a = 1;\n-b = 2;\n+b = 3;\n c = 4;\n"
    tgt = ["enum intstate_e event; /* Last event that occurred with this status */", "struct imxrt_can flexcans[] =",
           "regval |= QSPI_CR_ENABLE_BIT;"]
    for d in (comment, reflow, moved, short):
        (v,) = judge(d, tgt)
        assert v.status == "skip", d


ADD = ("@@ -20,5 +20,6 @@\n   regs_write_control(priv, CTRL_STOP);\n   regs_write_count(priv, 0);\n"
       "   regs_clear_flags(priv, FLAG_ALL);\n+  regs_trigger_update(priv, UPDATE);\n   regs_write_control(priv, CTRL_START);\n")


def test_add_needs_the_gap_to_be_in_the_same_place():
    ctx = ["regs_write_control(priv, CTRL_STOP);", "regs_write_count(priv, 0);", "regs_clear_flags(priv, FLAG_ALL);"]
    (gap,) = judge(ADD, ctx + ["regs_write_control(priv, CTRL_START);"])
    assert (gap.status, gap.kind, len(gap.ctx)) == ("candidate", "add", 3)
    # the context lines are there, but a different line follows: the copy already has something else here
    (other,) = judge(ADD, ctx + ["regs_do_something_else(priv);", "regs_write_control(priv, CTRL_START);"])
    assert (other.status, other.why) == ("diverged", "unanchored")
    # the following line does not exist in the copy at all (each calls its own API): still a candidate
    (vendor,) = judge(ADD, ctx + ["vendor_specific_start_call(priv);"])
    assert vendor.status == "candidate"
    # context scattered over the file; fewer than 2 context lines
    (scattered,) = judge(ADD, [ctx[0], "unrelated_line_number_one();", ctx[1], ctx[2], "regs_write_control(priv, CTRL_START);"])
    assert scattered.status == "diverged"
    assert judge(ADD, [ctx[2]])[0].status == "diverged"
    # the copy already has the added line
    assert judge(ADD, ctx + ["regs_trigger_update(priv, UPDATE);"])[0].status == "has_fix"


def test_still_fixed_looks_at_position():
    (v,) = judge(REPL, BEFORE)
    mk = lambda lines: hunks.make_view([CODE(s) for s in lines], N.valid_c_change)  # noqa: E731
    # the fix was reverted, but the same sentence appears elsewhere in the file: looking only for the text is wrong
    reverted = mk(BEFORE + ["totally_different_function();", "if (qh->epinfo != epinfo)", "another_unrelated_call();"])
    assert hunks.still_fixed(v, mk(AFTER), N.valid_c_change)
    assert not hunks.still_fixed(v, reverted, N.valid_c_change)
    assert not hunks.still_fixed(v, None, N.valid_c_change)


# ------------------------------------------------------------------ the whole pipeline on a small temporary repository


class Tiny(Codebase):
    """Three "drivers" a, b and c, each with a copied drv.c. Only whitespace is normalized."""

    name = "tiny"
    strict_replace = False

    def __init__(self, root, sha):
        super().__init__()
        self._root, self.repos = root, {"t/r": sha}

    def root(self, repo):
        return self._root

    def in_scope(self, repo, path):
        return path.endswith(".c")

    def unit_of(self, repo, path):
        return path.split("/")[0]

    def files(self):
        return [SrcFile("t/r", p.relative_to(self._root).as_posix(), p.parent.name)
                for p in sorted(self._root.glob("*/drv.c"))]


def body(**over):
    lines = [f"static int driver_function_number_{i:02d}(struct dev_s *dev);" for i in range(60)]
    for i, s in over.items():
        lines[int(i[1:])] = s
    return "\n".join(lines) + "\n"


@pytest.fixture
def tiny(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()

    def git(*a):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a], cwd=root,
                              check=True, capture_output=True, text=True).stdout.strip()

    def write(unit, **over):
        (root / unit).mkdir(exist_ok=True)
        (root / unit / "drv.c").write_text(body(**over))

    git("init", "-q")
    write("a")
    write("b", L30="static int already_fixed_in_b_from_the_start(void);", L50="static int b_went_its_own_way_here(void);")
    write("c")
    git("add", "-A"), git("commit", "-q", "-m", "init")
    # 1. a and b changed together (fixed in the same commit); c unchanged (candidate)
    write("a", L10="static int fixed_line_ten_in_both_a_and_b(void);")
    write("b", L10="static int fixed_line_ten_in_both_a_and_b(void);", L30="static int already_fixed_in_b_from_the_start(void);",
          L50="static int b_went_its_own_way_here(void);")
    git("add", "-A"), git("commit", "-q", "-m", "drv: fix line ten")
    # 2. only a changes; b had the fixed code from the start (copy already has it); c still old (candidate)
    write("a", L10="static int fixed_line_ten_in_both_a_and_b(void);", L30="static int already_fixed_in_b_from_the_start(void);")
    git("add", "-A"), git("commit", "-q", "-m", "drv: Fix line thirty")
    # 3. only a changes; b was written differently here long ago (diverged); c still old (candidate)
    write("a", L10="static int fixed_line_ten_in_both_a_and_b(void);", L30="static int already_fixed_in_b_from_the_start(void);",
          L50="static int fixed_line_fifty_only_in_a(void);")
    git("add", "-A"), git("commit", "-q", "-m", "fix line fifty")
    # 4. not a bug fix: a rewrites line 10 again (the first fix is no longer in a)
    write("a", L10="static int line_ten_rewritten_again_later(void);", L30="static int already_fixed_in_b_from_the_start(void);",
          L50="static int fixed_line_fifty_only_in_a(void);")
    git("add", "-A"), git("commit", "-q", "-m", "drv: rework line ten")
    # 5. delete-only: a drops line 40; b and c still have it (the C rules do not count it as a candidate)
    lines = body(L10="static int line_ten_rewritten_again_later(void);", L30="static int already_fixed_in_b_from_the_start(void);",
                 L50="static int fixed_line_fifty_only_in_a(void);").split("\n")
    del lines[40]
    (root / "a" / "drv.c").write_text("\n".join(lines))
    git("add", "-A"), git("commit", "-q", "-m", "drv: fix by dropping line forty")
    monkeypatch.setattr(gitio, "git_dir", lambda repo: root / ".git")
    monkeypatch.setattr(paths, "repo_dir", lambda repo: root)
    monkeypatch.setattr(paths, "OUT", tmp_path / "out")
    monkeypatch.setattr(paths, "JUDGMENTS", tmp_path / "judgments")
    return Tiny(root, git("rev-parse", "HEAD"))


def run_steps(cb):
    """The same as what pipeline.run_steps does for one codebase."""
    from fixprop import candidates, diverged, labels

    files = pairs.load_files(cb)
    stats = census.step_pairs(cb, files)
    st, rows = census.step_status(cb, files)
    recs = candidates.run(cb, rows)
    bad = labels.merge(cb, recs)
    diverged.analyse(cb, files)
    bad += labels.merge_diverged(cb, diverged.sample(cb, files))
    return stats, st, rows, recs, bad


def write_labels(name, labs, file="labels.jsonl"):
    f = paths.JUDGMENTS / name / file
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("".join(json.dumps(x) + "\n" for x in labs), encoding="utf-8")


def test_pipeline_gives_all_four_statuses(tiny):
    stats, st, rows, recs, bad = run_steps(tiny)
    assert stats["pairs"]["30"] == 3 and stats["units"] == 3 and bad == 0
    assert (st["commits_no_merges"], st["fix_commits"], st["fix_touching_paired_files"]) == (6, 4, 4)
    assert rows == census.read_jsonl(census.out_dir(tiny) / "fix_status.jsonl")
    got = {(r["subject"], r["target_unit"]): r for r in rows if r["source_unit"] == "a"}
    assert got[("drv: fix line ten", "b")]["status"] == "same_commit"
    ten_c = got[("drv: fix line ten", "c")]
    assert ten_c["status"] == "candidate" and ten_c["source_still_fixed"] is False
    assert ten_c["hunks"][0]["target_lines"] == [11]
    assert got[("drv: Fix line thirty", "b")]["status"] == "has_fix"
    thirty_c = got[("drv: Fix line thirty", "c")]
    assert thirty_c["status"] == "candidate" and thirty_c["source_still_fixed"] is True
    assert got[("fix line fifty", "b")]["status"] == "diverged"
    assert st["pair_status"] == {"same_commit": 2, "has_fix": 1, "candidate": 3, "diverged": 1, "delete_only": 2}
    forty = {r["target_unit"]: r["status"] for r in rows if r["subject"] == "drv: fix by dropping line forty"}
    assert forty == {"b": "delete_only", "c": "delete_only"}  # delete-only: neither a candidate nor diverged
    # the first fix also changed b: a rewrote it later, b still has it, so the fixed side of (commit, c) still has the fix
    b_to_c = next(r for r in rows if r["source_unit"] == "b" and r["target_unit"] == "c")
    assert b_to_c["status"] == "candidate" and b_to_c["source_still_fixed"] is True
    assert (st["candidate_pairs"], st["candidate_pairs_still_fixed"]) == (3, 3)
    # none of the three candidates has been read: numbered from 001 in commit order
    assert sorted(r["id"] for r in recs) == ["tiny-001", "tiny-002", "tiny-003"]
    assert all(r["label"] is None for r in census.read_jsonl(census.out_dir(tiny) / "candidates.jsonl"))


def test_judgments_are_merged_checked_and_counted(tiny):
    """Judgments are merged; cited lines must match verbatim; flagged pairs are listed; counts are exact once everything
    is read and scaled up before that."""
    from fixprop import missing, packets

    _, _, _, recs, _ = run_steps(tiny)
    by_subject = {r["subject"]: r for r in recs}
    key = lambda r: {k: r[k] for k in ("id", "commit", "target_repo", "target_unit")}  # noqa: E731
    ref = [{"file": "c/drv.c", "line": 31, "quote": "driver_function_number_30"}]
    real = {**key(by_subject["drv: Fix line thirty"]), "label": "missing", "confidence": "high",
            "evidence": "c still has the old line 30", "reader": "test", "evidence_refs": ref, "port": "verbatim",
            "impact": "runtime", "where": {"file": "c/drv.c", "line": 31}, "title": "Line 30 was not carried over",
            "summary": "a fixed line 30; c still has the old line."}
    other = {**key(by_subject["drv: fix line ten"]), "label": "not_a_fix", "confidence": "high", "evidence": "only a rename",
             "reader": "test", "evidence_refs": ref}
    write_labels("tiny", [real, other])
    _, _, _, recs, bad = run_steps(tiny)
    assert bad == 0
    assert {r["id"]: r["label"] for r in recs} == {real["id"]: "missing", other["id"]: "not_a_fix",
                                                   by_subject["fix line fifty"]["id"]: None}
    c = missing.count_candidates("tiny")  # two of three read, one flagged: scaled up, with an interval
    assert (c["candidates"], c["read"], c["unread"], c["missing"], c["est"]) == (3, 2, 1, 1, 1.5) and c["lo"] <= 1.5 <= c["hi"]
    assert c["labels"] == {"missing": 1, "not_applicable": 0, "not_a_fix": 1, "fixed_differently": 0, "unsure": 0}

    (rec,) = missing.collect(tiny)
    assert (rec["id"], rec["found_in"], rec["port"], rec["impact"], rec["reader"]) == (real["id"], "candidate", "verbatim", "runtime", "test")
    assert rec["title"] == "Line 30 was not carried over" and rec["fix"]["subject"] == "drv: Fix line thirty"
    assert (rec["copy"]["file"], rec["copy"]["line"]) == ("c/drv.c", 31) and rec["copy"]["url"].endswith("/c/drv.c#L31")
    (place,) = rec["places"]
    (change,) = place["changes"]
    assert place["target"] == "c/drv.c" and change["target_lines"] == [31]
    assert "+static int already_fixed_in_b_from_the_start(void);" in change["diff"]
    assert "   31> static int driver_function_number_30(struct dev_s *dev);" in change["target_now"]

    # the reading material for the unread pair: the hunk and the copy's current code, and no judgment
    unread = by_subject["fix line fifty"]
    text = packets.candidate_packet(tiny, unread["id"], (unread["commit"], "t/r", "c"), census.unit_level(
        census.read_jsonl(census.out_dir(tiny) / "fix_status.jsonl"))[(unread["commit"], "t/r", "c")])
    assert f"ID: {unread['id']}" in text and "+static int fixed_line_fifty_only_in_a(void);" in text
    assert "   51> static int driver_function_number_50(struct dev_s *dev);" in text and "missing" not in text

    # the third one is read too: everything read, so the flagged count is exact
    third = {**key(unread), "label": "not_applicable", "confidence": "low", "evidence": "c does not need it",
             "evidence_refs": ref, "reader": "test"}
    write_labels("tiny", [real, other, third])
    assert run_steps(tiny)[4] == 0
    c = missing.count_candidates("tiny")
    assert (c["read"], c["unread"], c["missing"], c["est"], c["lo"], c["hi"]) == (3, 0, 1, 1.0, 1, 1)

    # cited line does not match, a flagged pair lacks a field, where is not a cited line, a judgment for a pair that is
    # no longer a candidate: all reported
    bad_real = {**real, "evidence_refs": [{"file": "c/drv.c", "line": 5, "quote": "driver_function_number_30"}]}
    del bad_real["port"]
    stale = {**other, "id": "tiny-099", "target_unit": "zzz"}
    write_labels("tiny", [bad_real, other, third, stale])
    assert run_steps(tiny)[4] == 4


def test_judgment_checks_reject_chinese_text_and_bad_fields(tiny):
    """Everything a reader writes must be in English; confidence must be valid; light impact needs a note."""
    from fixprop import labels

    run_steps(tiny)
    ref = [{"file": "c/drv.c", "line": 31, "quote": "driver_function_number_30"}]
    good = {"label": "missing", "confidence": "high", "evidence": "c still has the old line", "evidence_refs": ref,
            "port": "verbatim", "impact": "runtime", "where": {"file": "c/drv.c", "line": 31}, "title": "Old line 30",
            "summary": "a fixed it; c did not."}
    check = lambda lab: labels.check_one("x", "t/r", lab, labels.LABELS)  # noqa: E731
    assert check(good) == []
    han = "".join(map(chr, (0x4E2D, 0x6587)))  # two Chinese characters
    assert any("evidence must be in English" in p for p in check({**good, "evidence": "old line " + han}))
    assert any("title must be in English" in p for p in check({**good, "title": han}))
    assert any("quote only the code" in p for p in check({**good, "evidence_refs": [{**ref[0], "quote": han}]}))
    assert any("confidence" in p for p in check({**good, "confidence": "sure"}))
    assert any("impact_note" in p for p in check({**good, "impact": "light"}))
    assert check({**good, "impact": "light", "impact_note": "log text only"}) == []


def test_batches_are_even_and_prompts_are_filled():
    """Packets are cut into batches of at most MAX_BATCH whose sizes differ by at most one; the prompts are complete
    and contain no Chinese text."""
    from fixprop import labels, packets

    index = ([{"id": f"x-{i:03d}", "codebase": "x", "kind": "candidate"} for i in range(1, 20)]
             + [{"id": f"x-D{i:02d}", "codebase": "x", "kind": "diverged"} for i in range(1, 11)])
    b = packets.split_batches(index)
    assert {k: len(v) for k, v in b.items()} == {"x-c01": 7, "x-c02": 6, "x-c03": 6, "x-d01": 5, "x-d02": 5}
    assert [it["id"] for it in b["x-c02"]] == [f"x-{i:03d}" for i in range(8, 14)] and index[0]["batch"] == "x-c01"
    for template in (packets.TASK, packets.TASK_DIVERGED):
        p = template.format(batch="x-c01", ids="x-001, x-002", dir="work/read", output="work/read/results/x-c01.jsonl",
                            tmp="work/read/tmp/x-c01/")
        assert "x-001, x-002" in p and "--verify work/read/results/x-c01.jsonl --batch x-c01" in p and "{" not in p
    for text in (packets.GUIDE, packets.GUIDE_DIVERGED, packets.TASK, packets.TASK_DIVERGED):
        assert not labels.CJK.search(text)


def test_collect_copies_checked_results_into_judgments(tiny, tmp_path):
    """--collect checks every batch first and writes nothing while a pair is missing; then it copies the judgments
    with their keys and the reader's model ID, and they merge without problems."""
    from fixprop import packets

    _, _, _, recs, _ = run_steps(tiny)
    out = tmp_path / "read"
    index = [{"id": r["id"], "codebase": "tiny", "kind": "candidate", "commit": r["commit"], "target_repo": r["target_repo"],
              "target_unit": r["target_unit"], "read": False} for r in recs]
    packets.split_batches(index)
    census.write_jsonl(out / "index.jsonl", index)
    ref = [{"file": "c/drv.c", "line": 31, "quote": "driver_function_number_30"}]
    lines = [{"id": r["id"], "label": "not_a_fix", "confidence": "low", "evidence": "only a rename", "evidence_refs": ref}
             for r in recs]
    census.write_jsonl(out / "results" / "tiny-c01.jsonl", lines[:2])
    assert packets.collect(out, "model-x") > 0 and not (paths.JUDGMENTS / "tiny" / "labels.jsonl").exists()
    census.write_jsonl(out / "results" / "tiny-c01.jsonl", lines)
    assert packets.collect(out, "model-x") == 0
    got = census.read_jsonl(paths.JUDGMENTS / "tiny" / "labels.jsonl")
    assert [x["id"] for x in got] == sorted(r["id"] for r in recs) and {x["reader"] for x in got} == {"model-x"}
    assert run_steps(tiny)[4] == 0


def test_pipeline_output_is_identical_when_rerun(tiny, tmp_path, monkeypatch):
    """The same inputs twice give byte-identical files (nothing like timestamps or durations in them)."""
    from fixprop import pipeline

    outs = []
    for i in (1, 2):
        monkeypatch.setattr(paths, "OUT", tmp_path / f"out{i}")
        run_steps(tiny)
        pipeline.write_sums(paths.OUT)
        outs.append(paths.OUT)
    assert pipeline.compare_dirs(*outs) == []
    assert sorted(census.read_json(outs[0] / "tiny" / "stats.json")) == ["pairs", "status"]
    assert "seconds" not in (outs[0] / "tiny" / "stats.json").read_text()


def test_moved_file_tie_picks_the_first_path():
    """Finding where a moved file is now, by content: on a tie, the path that sorts first wins, whatever comes first."""
    lines = [f"static int some_long_function_name_{i:02d}(void);" for i in range(40)]

    def fl(path, ls=lines):
        return pairs.FileLines(SrcFile("t/r", path, path.split("/")[0]), ls, frozenset(ls), frozenset(ls), "x", "")

    class CB(Codebase):
        name = "tiny"

    blob = "\n".join(lines).encode()
    for order in (["b/drv.c", "a/drv.c", "c/drv.c"], ["c/drv.c", "b/drv.c", "a/drv.c"]):
        succ = census.Successors(CB(), {("t/r", p): fl(p) for p in order})
        assert succ.resolve("t/r", "old/drv.c", blob) == ("t/r", "a/drv.c")
    # more shared lines win, whatever the path
    succ = census.Successors(CB(), {("t/r", "a/drv.c"): fl("a/drv.c", lines[:35]), ("t/r", "z/drv.c"): fl("z/drv.c")})
    assert succ.resolve("t/r", "old/drv.c", blob) == ("t/r", "z/drv.c")


# ------------------------------------------------------------------ diverged groups; estimates; generated documents


def test_enclosing_function():
    from fixprop import diverged

    c = ["static int up_setup(struct uart_dev_s *dev);", "", "static int stm32_i2c_isr_process(struct stm32_i2c_priv_s *priv)",
         "{", "  uint32_t status;", "  if (status & FLAG)", "    {", "      stm32_i2c_putreg(priv, REG, 0);", "    }",
         "  return OK;", "}"]
    assert diverged.enclosing_function(c, 8, "c") == "stm32_i2c_isr_process"  # skips prototypes (ending in ;) and if
    rs = ["impl<'d> Uart<'d> {", "    pub async fn write(&mut self, buf: &[u8]) -> Result<(), Error> {",
          "        let r = self.info.regs;", "        r.cr1().modify(|w| w.set_te(true));", "    }", "}"]
    assert diverged.enclosing_function(rs, 4, "rust") == "write"
    assert diverged.enclosing_function(["int x = 1;"], 1, "c") == ""
    (h,) = hunks.parse_diff("@@ -10,4 +10,4 @@\n a\n b\n-c\n+d\n e\n")
    assert diverged.first_change_line(h) == 12


def test_estimate_when_not_everything_is_read(tmp_path, monkeypatch):
    """40 of 300 read, 10 flagged: about 75, with a Wilson interval."""
    from fixprop import missing

    lo, hi = missing.wilson(10, 40)
    assert 0.14 < lo < 0.15 and 0.40 < hi < 0.41
    monkeypatch.setattr(paths, "OUT", tmp_path)
    mk = lambda lab, port=None: {"label": lab, "port": port, "impact": "runtime"}  # noqa: E731
    recs = [mk("missing", "verbatim")] * 6 + [mk("missing", "adapt")] * 4 + [mk("not_a_fix")] * 30 + [mk(None)] * 260
    census.write_jsonl(tmp_path / "x" / "candidates.jsonl", recs)
    c = missing.count_candidates("x")
    assert (c["candidates"], c["read"], c["unread"], c["missing"], c["est"]) == (300, 40, 260, 10, 75.0)
    assert c["lo"] < 75 < c["hi"] and c["by_port"]["verbatim"]["est"] == 45.0 and c["by_port"]["adapt"]["est"] == 30.0


def test_same_problem_counted_once():
    """The same problem reached from two fixes counts once among distinct problems; wrong markings are reported."""
    from fixprop import missing

    def rec(i, unit="u1", same=None):
        r = {"id": i, "codebase": "x", "copy": {"repo": "o/r", "unit": unit}}
        if same:
            r["same_problem_as"] = same
        return r

    found = [rec("x-001"), rec("x-002", same="x-001"), rec("x-003")]
    uniq, problems = missing.distinct(found)
    assert [r["id"] for r in uniq] == ["x-001", "x-003"] and problems == []
    bad = [rec("x-001"), rec("x-002", same="x-009"), rec("x-003", same="x-002"), rec("x-004", unit="u2", same="x-001")]
    _, problems = missing.distinct(bad)
    assert len(problems) == 3  # points at nothing; points at a repeat; points into another driver


def test_doc_blocks_follow_the_results(tmp_path, monkeypatch):
    from fixprop import readme

    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(readme, "blocks", lambda out: {"README.md": {"summary": "| result | 3 |"},
                                                       "judgments/results.md": {"results": "- 3 flagged", "list": "| a-001 | ... |"}})
    (tmp_path / "judgments").mkdir()
    f, g = tmp_path / "README.md", tmp_path / "judgments" / "results.md"
    f.write_text("start\n<!-- BEGIN:summary -->\nold\n<!-- END:summary -->\nend\n")
    g.write_text("<!-- BEGIN:results -->\n<!-- END:results -->\nmiddle\n<!-- BEGIN:list -->\n<!-- END:list -->\n")
    census.write_json(tmp_path / "summary.json", {})
    assert len(readme.stale(tmp_path)) == 3
    assert readme.sync(tmp_path) == ["README.md", "judgments/results.md"] and readme.sync(tmp_path) == []
    assert readme.stale(tmp_path) == []
    assert f.read_text() == "start\n<!-- BEGIN:summary -->\n| result | 3 |\n<!-- END:summary -->\nend\n"


def test_readme_human_column_is_todo_until_review_starts():
    from fixprop import readme

    d = {"total": 2, "by_impact": {"runtime": 1, "light": 1}, "by_port": {"verbatim": 1, "adapt": 1}}
    s = {"confirmed": {"total": 3, "distinct": d}, "human_review": {"reviewed": 0, "claude_missing": {"total": 3, "reviewed": 0}}}
    out = readme.human_block(s, [], [])
    assert out.count("TODO") == 5 and "0 of 3 flagged pairs" in out
    rows = [{"id": "a-001", "impact": "runtime", "port": "verbatim"}, {"id": "a-002", "impact": "light", "port": "adapt"},
            {"id": "a-003", "impact": "runtime", "port": "verbatim", "same_problem_as": "a-001"}]
    final = [{"id": "a-001", "final": "missing"}, {"id": "a-002", "final": "not_missing"}, {"id": "a-003", "final": "missing"}]
    s["human_review"] = {"reviewed": 3, "final_missing_distinct": 1, "final": {"conflict": 0},
                         "claude_missing": {"total": 3, "reviewed": 3, "human_missing": 2}}
    out = readme.human_block(s, rows, final)
    assert "TODO" not in out and "in progress" not in out
    assert "| Fix–copy pairs that are missed fixes | 3 | 2 |" in out and "| Distinct problems | 2 | 1 |" in out
    assert "| Distinct problems that affect runtime behaviour | 1 | 1 |" in out and "2 of 3 reviewed" in out


def test_review_status_says_todo_until_review_starts():
    from fixprop import readme

    hs, hb = readme._human_status({"reviewed": 0, "claude_missing": {"total": 136, "reviewed": 0}})
    assert "TODO" in hs and "0 of 136" in hs and "TODO" in hb
    h = {"reviewed": 3, "final": {"missing": 2, "not_missing": 1, "unsure": 0, "conflict": 0}, "final_missing_distinct": 2,
         "claude_missing": {"total": 136, "reviewed": 3, "human_missing": 2, "human_not_missing": 1, "human_unsure": 0},
         "claude_other_reviewed": 0}
    hs, hb = readme._human_status(h)
    assert hs.startswith("in progress") and "2 pairs confirmed" in hb and "not finished" in hb


def test_summary_numbers_must_agree():
    """Disagreeing numbers in the summary are reported: change one number of the real summary and check."""
    import copy

    from fixprop import missing

    f = paths.ROOT / "benchmark" / "summary.json"
    if not f.is_file():
        pytest.skip("no benchmark/summary.json")
    s = census.read_json(f)
    assert missing.consistency(s) == []
    bad = copy.deepcopy(s)
    bad["funnel"]["nuttx"]["diverged"] += 7
    assert any("checked = sum of the outcomes" in p for p in missing.consistency(bad))


# ------------------------------------------------------------------ human review


def _review_env(tiny, monkeypatch):
    """Run the pipeline on the small repository with two judgments, one flagged, and point review at this codebase."""
    from fixprop import review

    _, _, _, recs, _ = run_steps(tiny)
    by = {r["subject"]: r for r in recs}
    ref = [{"file": "c/drv.c", "line": 31, "quote": "driver_function_number_30"}]
    key = lambda r: {k: r[k] for k in ("id", "commit", "target_repo", "target_unit")}  # noqa: E731
    write_labels("tiny", [
        {**key(by["drv: Fix line thirty"]), "label": "missing", "confidence": "high", "evidence": "c still has the old line 30",
         "reader": "test", "evidence_refs": ref, "port": "verbatim", "impact": "runtime",
         "where": {"file": "c/drv.c", "line": 31}, "title": "Line 30 was not carried over",
         "summary": "a fixed line 30; c still has the old line."},
        {**key(by["drv: fix line ten"]), "label": "not_a_fix", "confidence": "high", "evidence": "only a rename",
         "reader": "test", "evidence_refs": ref}])
    run_steps(tiny)
    monkeypatch.setattr(review, "ORDER", ("tiny",))
    monkeypatch.setattr(review, "get", lambda name: tiny)
    monkeypatch.setattr(review, "all_repos", lambda: dict(tiny.repos))
    return review, by


def test_review_page_items_show_fix_and_unfixed_code(tiny, monkeypatch):
    review, by = _review_env(tiny, monkeypatch)
    items = {x["id"]: x for x in review.build_items()}
    it = items[by["drv: Fix line thirty"]["id"]]
    assert it["group"] == "claude_missing" and it["claude"]["label_text"] == "Flagged as a missed fix"
    assert it["claude"]["title"] == "Line 30 was not carried over"
    (place,) = it["places"]
    lines = [t for h in place["fix"] for _, t in h["lines"]]
    assert "static int already_fixed_in_b_from_the_start(void);" in lines  # the fix: the new code
    assert len(lines) > 7  # more than 3 lines of context
    assert place["marks"] == [31] and any("driver_function_number_30" in ln for b in place["copy"] for ln in b["lines"])
    assert items[by["drv: fix line ten"]["id"]]["group"] == "other" and items[by["fix line fifty"]["id"]]["claude"]["label"] is None
    assert review.build_items() == list(items.values())  # the same inputs give the same content


def test_review_store_saves_validates_and_clears(tiny, monkeypatch):
    review, by = _review_env(tiny, monkeypatch)
    i = by["drv: Fix line thirty"]["id"]
    s = review._Store("alice", {i})
    assert s.save({"id": i, "verdict": "not_missing"}) != []  # not a missed fix needs a reason
    assert s.save({"id": "nope", "verdict": "missing"}) != []
    assert s.save({"id": i, "verdict": "not_missing", "reason": "not_applicable", "note": "different chip", "saw_llm": True}) == []
    assert s.save({"id": i, "verdict": "missing", "saw_llm": False}) == []  # a change replaces the line
    (row,) = census.read_jsonl(paths.JUDGMENTS / "review" / "alice.jsonl")
    assert (row["verdict"], row["reason"], row["saw_llm"]) == ("missing", None, False)
    assert s.save({"id": i, "verdict": None}) == [] and census.read_jsonl(paths.JUDGMENTS / "review" / "alice.jsonl") == []


def test_review_merge_builds_the_final_benchmark(tiny, monkeypatch):
    from fixprop import missing

    review, by = _review_env(tiny, monkeypatch)
    real, other = by["drv: Fix line thirty"]["id"], by["drv: fix line ten"]["id"]
    found = missing.collect(tiny)
    h, bad = review.merge(found)
    assert bad == 0 and h["reviewed"] == 0 and census.read_jsonl(paths.OUT / "final.jsonl") == []
    d = paths.JUDGMENTS / "review"
    d.mkdir(parents=True, exist_ok=True)
    (d / "alice.jsonl").write_text(json.dumps({"id": real, "verdict": "missing", "saw_llm": False}) + "\n" +
                                   json.dumps({"id": other, "verdict": "missing", "saw_llm": True}) + "\n")
    (d / "bob.jsonl").write_text(json.dumps({"id": real, "verdict": "missing"}) + "\n" +
                                 json.dumps({"id": other, "verdict": "not_missing", "reason": "not_a_fix"}) + "\n")
    h, bad = review.merge(found)
    final = {x["id"]: x for x in census.read_jsonl(paths.OUT / "final.jsonl")}
    assert bad == 0 and final[real]["final"] == "missing" and final[other]["final"] == "conflict"
    assert [r["reviewer"] for r in final[real]["reviews"]] == ["alice", "bob"]
    assert h["claude_missing"] == {"total": 1, "reviewed": 1, "human_missing": 1, "human_not_missing": 0,
                                   "human_unsure": 0, "human_conflict": 0}
    assert (h["claude_other_reviewed"], h["final"]["conflict"], h["final_missing_distinct"]) == (1, 1, 1)
    (d / "carol.jsonl").write_text(json.dumps({"id": real, "verdict": "maybe"}) + "\n")  # an invalid verdict is reported
    assert review.merge(found)[1] == 1


# ------------------------------------------------------------------ checking the data and the results


def test_checksums_and_dir_compare(tmp_path):
    from fixprop import pipeline

    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        (d / "sub").mkdir(parents=True)
        (d / "x.json").write_text("{}\n")
        (d / "sub" / "y.jsonl").write_text("hello\n")
        assert pipeline.write_sums(d) == 2
    assert pipeline.check_sums(a) == [] and pipeline.compare_dirs(a, b) == []
    (b / "sub" / "y.jsonl").write_text("changed\n")
    (b / "extra.txt").write_text("new\n")
    assert pipeline.check_sums(b) == ["extra file extra.txt", "changed file sub/y.jsonl"]
    diffs = pipeline.compare_dirs(a, b)
    assert any("extra.txt" in d for d in diffs) and any("sub/y.jsonl" in d for d in diffs)
    # all replaces a directory only if it does not exist, is empty, or is a results directory it wrote itself
    other = tmp_path / "other"
    other.mkdir()
    assert pipeline._is_results_dir(other) and pipeline._is_results_dir(a) and pipeline._is_results_dir(tmp_path / "none")
    (other / "notes.txt").write_text("someone else's files\n")
    assert not pipeline._is_results_dir(other)


def test_git_blob_id_matches_git(tmp_path):
    from fixprop import fetch

    f = tmp_path / "f.c"
    f.write_bytes(b"int main(void)\n{\n  return 0;\n}\n")
    want = subprocess.run(["git", "hash-object", str(f)], capture_output=True, text=True, check=True).stdout.strip()
    assert fetch.git_blob_id(f.read_bytes()) == want


def test_verify_data_reports_what_is_wrong(tiny, tmp_path, monkeypatch):
    """Checking the data: a changed file, a history that differs from the record, a commit not in the history."""
    from fixprop import fetch

    sha = tiny.repos["t/r"]
    monkeypatch.setattr(fetch, "todo_list", lambda which=None: [("t/r", sha, "rtos")])
    monkeypatch.setattr(fetch, "CODEBASE_OF", {"t/r": "nuttx"})
    monkeypatch.setattr(fetch, "get", lambda name: tiny)
    monkeypatch.setattr(fetch, "_gitlinks", lambda: None)
    monkeypatch.setattr(paths, "LOCK", tmp_path / "lock.json")
    census.write_json(paths.LOCK, {"t/r": {"commit": sha, "cut_at": [], "commits": 6}})
    assert fetch.verify() == []
    census.write_json(paths.LOCK, {"t/r": {"commit": sha, "cut_at": [], "commits": 5}})
    assert any("the history differs from the record" in p for p in fetch.verify())
    census.write_json(paths.LOCK, {"t/r": {"commit": sha, "cut_at": [], "commits": 6}})
    (tiny._root / "c" / "drv.c").write_text("tampered\n")
    assert any("source file differs from the pinned commit: c/drv.c" in p for p in fetch.verify())
    monkeypatch.setattr(fetch, "todo_list", lambda which=None: [("t/r", "f" * 40, "rtos")])
    assert any("does not contain the pinned commit" in p for p in fetch.verify())


def test_repo_list_and_submodules_agree():
    """data/repos.toml, data/repos.lock.json and .gitmodules list the same repositories."""
    from fixprop import codebases, fetch

    repos = codebases.all_repos()
    assert len(codebases.rust_repos()) == 37 and len(codebases.rtos_repos()) == 3
    lock = census.read_json(paths.LOCK)
    assert set(lock) == set(repos) and all(lock[r]["commit"] == c for r, c in repos.items())
    gm = (paths.ROOT / ".gitmodules").read_text(encoding="utf-8")
    for repo in repos:
        assert f'[submodule "data/repos/{repo}"]' in gm and f"url = https://github.com/{repo}.git" in gm
    assert gm.count("update = none") == 40
    links = fetch._gitlinks()
    if links is not None:  # when this project is a git repository: the registered submodule commits match the list
        assert links == {f"data/repos/{r}": c for r, c in repos.items()}


def test_cli_commands():
    import sys

    out = subprocess.run([sys.executable, "-m", "fixprop", "--help"], capture_output=True, text=True,
                         cwd=paths.ROOT).stdout
    for cmd in ("fetch", "all", "check", "review", "packets"):
        assert cmd in out
