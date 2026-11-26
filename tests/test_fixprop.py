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


def test_commits_that_take_a_commit_back_are_not_bug_fixes():
    revert = census.REVERT.match
    assert revert('Revert "drivers: spi: fix TX-only mode"') and revert('revert "fix: reference project dir"')
    # written by hand, and a fix may be part of it: stays in
    assert not revert("Reverted previous change to bounded loops; fixed") and not revert("drv: fix the revert path")


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
    (d,) = judge(REPL, ["something_else_entirely_here();"])
    assert (d.status, d.why) == ("diverged", "")


def test_the_place_of_the_change_decides_not_the_text_anywhere():
    """The same statement elsewhere in the file says nothing about the place of the change."""
    elsewhere = ["totally_different_function();", "if (qh->epinfo != epinfo)", "another_unrelated_call();"]
    # the pre-fix code is still in place, and the fixed line also exists in another function: still a candidate
    (both,) = judge(REPL, BEFORE + elsewhere)
    assert both.status == "candidate"
    # the fixed line exists only in another function, and the pre-fix code is not in place either: the script cannot tell
    (only_new,) = judge(REPL, ["flags = enter_critical_section();", "leave_critical_section(flags);"] + elsewhere)
    assert (only_new.status, only_new.why) == ("diverged", "unanchored")
    # a second copy of the function that was fixed does not hide the one that was not
    (twice,) = judge(REPL, AFTER + ["some_separator_between_functions();"] + BEFORE)
    assert twice.status == "candidate"


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
    # the copy already has the added line, after the context lines
    assert judge(ADD, ctx + ["regs_trigger_update(priv, UPDATE);"])[0].status == "has_fix"
    assert judge(ADD, ctx + ["regs_trigger_update(priv, UPDATE);", "regs_write_control(priv, CTRL_START);"])[0].status == "has_fix"
    # the gap is closed (the following line comes directly after the context), so the added line is not there,
    # although the same statement exists further down: a candidate
    start, far = "regs_write_control(priv, CTRL_START);", ["unrelated_line_in_between();", "regs_trigger_update(priv, UPDATE);"]
    assert judge(ADD, ctx + [start] + far)[0].status == "candidate"
    # the gap cannot be seen (the copy has no such following line) and the added line exists somewhere: cannot tell
    (unseen,) = judge(ADD, ctx + ["vendor_specific_start_call(priv);"] + far)
    assert (unseen.status, unseen.why) == ("diverged", "unanchored")


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
    write("c", L20="static int fixed_line_ten_in_both_a_and_b(void);")  # the line of fix 1, but at another place
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
    # 6. a commit that takes a change back: its message says "fix", but it is not a bug fix
    lines[54] = "static int line_fifty_five_put_back(void);"
    (root / "a" / "drv.c").write_text("\n".join(lines))
    git("add", "-A"), git("commit", "-q", "-m", 'Revert "drv: fix line fifty-five"')
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
    sample = diverged.sample(cb, files)
    bad += labels.merge_diverged(cb, sample)
    labels.mark_repeats(cb, recs, sample)
    return stats, st, rows, recs, bad


def transcript_of(cb, x, **over):
    """The transcript a judgment x would have come from: the current rules and packet, x accepted, some token usage."""
    from fixprop import labels, packets, reader

    agg = census.unit_level(census.read_jsonl(census.out_dir(cb) / "fix_status.jsonl"))
    sample = {r["id"]: r for r in census.read_jsonl(census.out_dir(cb) / "diverged_sample.jsonl")}
    key = (x["commit"], x["target_repo"], x["target_unit"])
    if x["id"] in sample:
        rules, packet = packets.GUIDE_DIVERGED, packets.diverged_packet(cb, sample[x["id"]], agg[key])
    elif key in agg:
        rules, packet = packets.GUIDE, packets.candidate_packet(cb, x["id"], key, agg[key])
    else:  # a judgment of a pair that does not exist any more
        rules, packet = packets.GUIDE, ""
    return {"format": reader.FORMAT, "id": x["id"], "codebase": cb.name, "kind": "diverged" if x["id"] in sample else "candidate",
            "commit": x["commit"], "target_repo": x["target_repo"], "target_unit": x["target_unit"], "endpoint": "",
            "request": {"model": x["reader"], "max_tokens": 1000, "reasoning": {"effort": "medium"}},
            "messages": [{"role": "system", "content": rules}, {"role": "user", "content": packet}], "calls": [],
            "outcome": {"status": "accepted", "rejected_submissions": 0,
                        "judgment": {k: x[k] for k in labels.JUDGMENT_FIELDS if k in x}},
            "usage": {"calls": 2, "prompt_tokens": 100, "cached_tokens": 40, "cache_write_tokens": 0,
                      "completion_tokens": 10, "reasoning_tokens": 3, "cost": 0.01}, **over}


def write_labels(cb, labs, file="labels.jsonl"):
    """Write a judgment file the way `read --collect` does: every judgment with the transcript it came from."""
    from fixprop import reader

    rows = []
    for x in labs:
        f = paths.JUDGMENTS / cb.name / "transcripts" / f"{x['id']}.json.gz"
        reader.save(f, transcript_of(cb, x))
        rows.append({**x, "transcript_sha256": reader.sha256(f)})
    f = paths.JUDGMENTS / cb.name / file
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")


def test_pipeline_gives_all_four_statuses(tiny):
    stats, st, rows, recs, bad = run_steps(tiny)
    assert stats["pairs"]["30"] == 3 and stats["units"] == 3 and bad == 0
    assert (st["commits_no_merges"], st["fix_commits"], st["fix_touching_paired_files"]) == (7, 4, 4)
    assert st["revert_commits_left_out"] == 1 and not any(r["subject"].startswith("Revert") for r in rows)
    assert rows == census.read_jsonl(census.out_dir(tiny) / "fix_status.jsonl")
    got = {(r["subject"], r["target_unit"]): r for r in rows if r["source_unit"] == "a"}
    assert got[("drv: fix line ten", "b")]["status"] == "same_commit"
    ten_c = got[("drv: fix line ten", "c")]
    assert ten_c["status"] == "candidate" and ten_c["source_still_fixed"] is False
    # c has the fixed line too, but 10 lines further down: the place of the change is as it was before the fix
    assert ten_c["hunks"][0]["target_lines"] == [11] and ten_c["hunks"][0]["added_elsewhere"] == [21]
    assert got[("drv: Fix line thirty", "b")]["status"] == "has_fix"
    thirty_c = got[("drv: Fix line thirty", "c")]
    assert thirty_c["status"] == "candidate" and thirty_c["source_still_fixed"] is True
    assert got[("fix line fifty", "b")]["status"] == "diverged"
    assert thirty_c["hunks"][0]["added_elsewhere"] == []
    assert st["pair_status"] == {"same_commit": 2, "has_fix": 1, "candidate": 5, "diverged": 1}
    forty = {r["target_unit"]: (r["status"], r["hunks"][0]["kind"]) for r in rows if r["subject"] == "drv: fix by dropping line forty"}
    assert forty == {"b": ("candidate", "delete"), "c": ("candidate", "delete")}  # the removed line is still in place
    # the first fix also changed b: a rewrote it later, b still has it, so the fixed side of (commit, c) still has the fix
    b_to_c = next(r for r in rows if r["source_unit"] == "b" and r["target_unit"] == "c")
    assert b_to_c["status"] == "candidate" and b_to_c["source_still_fixed"] is True
    assert (st["candidate_pairs"], st["candidate_pairs_still_fixed"], st["candidate_commits"]) == (5, 5, 4)
    # none of the five candidates has been read: numbered from 001 in commit order
    assert sorted(r["id"] for r in recs) == ["tiny-001", "tiny-002", "tiny-003", "tiny-004", "tiny-005"]
    assert all(r["label"] is None for r in census.read_jsonl(census.out_dir(tiny) / "candidates.jsonl"))
    # the two candidates of the delete-only fix come from the same commit and name each other; the others stand alone
    same = {r["id"]: r["same_fix"] for r in recs}
    b40, c40 = sorted(r["id"] for r in recs if r["subject"] == "drv: fix by dropping line forty")
    assert same[b40] == [c40] and same[c40] == [b40] and sum(1 for v in same.values() if not v) == 3


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
    write_labels(tiny, [real, other])
    _, _, _, recs, bad = run_steps(tiny)
    assert bad == 0
    assert {r["id"]: r["label"] for r in recs if r["label"]} == {real["id"]: "missing", other["id"]: "not_a_fix"}
    c = missing.count_candidates("tiny")  # two of five read, one flagged
    assert (c["candidates"], c["read"], c["unread"], c["missing"]) == (5, 2, 3, 1)
    assert c["labels"] == {"missing": 1, "not_applicable": 0, "not_a_fix": 1, "fixed_differently": 0, "unsure": 0}
    assert c["by_port"] == {"verbatim": 1, "adapt": 0} and c["by_impact"] == {"runtime": 1, "light": 0}

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
    assert "also appear" not in text
    # the packet of fix 1 says that the line it adds is also in c, somewhere else
    ten = by_subject["drv: fix line ten"]
    text = packets.candidate_packet(tiny, ten["id"], (ten["commit"], "t/r", "c"), census.unit_level(
        census.read_jsonl(census.out_dir(tiny) / "fix_status.jsonl"))[(ten["commit"], "t/r", "c")])
    assert "Lines that this hunk adds also appear in the other side's c/drv.c, at line numbers [21]." in text

    # a third one is read too
    third = {**key(unread), "label": "not_applicable", "confidence": "low", "evidence": "c does not need it",
             "evidence_refs": ref, "reader": "test"}
    write_labels(tiny, [real, other, third])
    assert run_steps(tiny)[4] == 0
    c = missing.count_candidates("tiny")
    assert (c["read"], c["unread"], c["missing"], c["labels"]["not_applicable"]) == (3, 2, 1, 1)

    # cited line does not match, a flagged pair lacks a field, where is not a cited line, a judgment for a pair that is
    # no longer a candidate: all reported
    bad_real = {**real, "evidence_refs": [{"file": "c/drv.c", "line": 5, "quote": "driver_function_number_30"}]}
    del bad_real["port"]
    stale = {**other, "id": "tiny-099", "target_unit": "zzz"}
    write_labels(tiny, [bad_real, other, third, stale])
    assert run_steps(tiny)[4] == 4


def test_every_judgment_needs_the_conversation_it_came_from(tiny, monkeypatch):
    """A judgment counts only with its transcript: present, unchanged, of this pair and model, held under the current
    rules about the current material, and ending with exactly this judgment. The record carries the settings and usage."""
    from fixprop import missing, packets, reader

    _, _, _, recs, _ = run_steps(tiny)
    r = next(x for x in recs if x["subject"] == "drv: Fix line thirty")
    lab = {**{k: r[k] for k in ("id", "commit", "target_repo", "target_unit")}, "label": "not_a_fix", "confidence": "high",
           "evidence": "only a rename", "reader": "some/model",
           "evidence_refs": [{"file": "c/drv.c", "line": 31, "quote": "driver_function_number_30"}]}
    f = paths.JUDGMENTS / "tiny" / "transcripts" / f"{r['id']}.json.gz"
    write_labels(tiny, [lab])
    _, _, _, recs, bad = run_steps(tiny)
    rec = next(x for x in recs if x["id"] == r["id"])
    assert bad == 0 and rec["transcript"] == f"judgments/tiny/transcripts/{r['id']}.json.gz"
    assert rec["reading"] == {"effort": "medium", "max_tokens": 1000, "provider": None, "calls": 2, "prompt_tokens": 100,
                              "cached_tokens": 40, "cache_write_tokens": 0, "completion_tokens": 10, "reasoning_tokens": 3,
                              "cost": 0.01}

    def problems(transcript=None, **change):
        """Problems after replacing the transcript (None removes it) and changing fields of the judgment line."""
        write_labels(tiny, [lab])
        (row,) = census.read_jsonl(paths.JUDGMENTS / "tiny" / "labels.jsonl")
        if transcript is None:
            f.unlink()
        else:
            reader.save(f, transcript)
            row["transcript_sha256"] = reader.sha256(f)
        row.update(change)
        (paths.JUDGMENTS / "tiny" / "labels.jsonl").write_text(json.dumps({k: v for k, v in row.items() if v is not None}) + "\n")
        return run_steps(tiny)[4]

    good = transcript_of(tiny, lab)
    assert problems(good) == 0
    assert problems(None) == 1  # the transcript is gone
    assert problems(good, transcript_sha256=None) == 1  # the judgment does not name its transcript
    assert problems(good, transcript_sha256="0" * 64) == 1  # the transcript is not the one the judgment came from
    assert problems(good, label="unsure") == 1  # the judgment was edited after the conversation
    assert problems(good, reader="other/model") == 1  # another model than the one in the conversation
    assert problems({**good, "target_unit": "b"}) == 1  # the conversation of another pair
    assert problems({**good, "outcome": {"status": "failed", "reason": "gave up"}}) == 1  # no accepted judgment
    rules, packet = good["messages"]
    assert problems({**good, "messages": [{**rules, "content": packets.GUIDE + "One more rule."}, packet]}) == 1  # other rules
    assert problems({**good, "messages": [rules, {**packet, "content": packet["content"] + "one more hunk"}]}) == 1  # other material

    # the summary says which model read the pairs with which settings, and adds up the usage
    assert problems(good) == 0
    monkeypatch.setattr(missing, "ORDER", ("tiny",))
    assert missing.reading() == {
        "conversations": 1,
        "settings": [{"model": "some/model", "effort": "medium", "max_tokens": 1000, "provider": None, "conversations": 1}],
        "calls": 2, "prompt_tokens": 100, "cached_tokens": 40, "cache_write_tokens": 0, "completion_tokens": 10,
        "reasoning_tokens": 3, "cost": 0.01}


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


def test_reading_rules_are_complete_and_in_english():
    """The two sets of rules name every verdict and every field of the submission, and contain no Chinese text."""
    from fixprop import labels, packets

    for text, verdicts in ((packets.GUIDE, labels.LABELS), (packets.GUIDE_DIVERGED, labels.LABELS_DIVERGED)):
        assert all(v in text for v in verdicts) and all(f in text for f in labels.JUDGMENT_FIELDS)
        assert "submit_judgment" in text and not labels.CJK.search(text)


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


def test_diverged_pairs_are_counted_by_group_and_nothing_is_scaled_up(tmp_path, monkeypatch):
    from fixprop import missing

    monkeypatch.setattr(paths, "OUT", tmp_path)
    census.write_jsonl(tmp_path / "x" / "diverged.jsonl", [{"bucket": "near"}] * 50 + [{"bucket": "gone"}] * 30 + [{"bucket": "unknown"}] * 7)
    mk = lambda bucket, label: {"bucket": bucket, "label": label, "port": "adapt", "impact": "runtime"}  # noqa: E731
    census.write_jsonl(tmp_path / "x" / "diverged_sample.jsonl",
                       [mk("near", "same_bug"), mk("near", "already_ok"), mk("near", None), mk("gone", "not_a_fix")])
    d = missing.count_diverged("x")
    assert (d["diverged"], d["not_covered"], d["sampled"], d["read"], d["same_bug"]) == (87, 7, 4, 3, 1)
    assert d["buckets"]["near"] == {"total": 50, "sampled": 3, "read": 2, "same_bug": 1}
    assert d["buckets"]["far"] == {"total": 0, "sampled": 0, "read": 0, "same_bug": 0}
    assert d["by_port"] == {"verbatim": 0, "adapt": 1} and "est" not in d and "est" not in d["buckets"]["near"]


def test_pairs_that_cover_a_common_line_of_one_copy_are_one_problem(tmp_path, monkeypatch):
    """Flagged pairs in the same copy that cover a common line are one problem; the earliest fix is the one counted."""
    from fixprop import labels

    monkeypatch.setattr(paths, "OUT", tmp_path)

    def cand(i, date, lines, unit="u1", label="missing", file="drv.c"):
        return {"id": i, "date": date, "label": label, "target_repo": "o/r", "target_unit": unit,
                "files": [{"target": file, "target_lines": lines}]}

    def div(i, date, line, unit="u1", label="same_bug"):
        return {"id": i, "date": date, "label": label, "target_repo": "o/r", "target_unit": unit,
                "where": {"file": "drv.c", "line": line}}

    cands = [cand("x-001", "2024-05-01", [10, 11]),          # the later fix of the same lines: a repeat of x-002
             cand("x-002", "2024-01-01", [11, 12]),
             cand("x-003", "2024-06-01", [12, 30]),          # shares line 12 with x-002, and brings line 30 into the group
             cand("x-004", "2024-07-01", [30]),              # reaches the group only through x-003
             cand("x-005", "2024-01-01", [11], unit="u2"),   # same lines, another copy
             cand("x-006", "2024-01-01", [11], file="other.c"),   # same line numbers, another file
             cand("x-007", "2023-01-01", [11], label="not_a_fix"),  # not flagged
             cand("x-008", "2024-01-01", [50]), cand("x-009", "2024-01-01", [50])]  # same day: the lower ID is counted
    sample = [div("x-D01", "2023-06-01", 11),                # the earliest fix of all: the one counted for the group
              div("x-D02", "2024-01-01", 99), div("x-D03", "2024-01-01", 11, label="already_ok")]
    cb = type("Cb", (), {"name": "x"})()
    assert labels.mark_repeats(cb, cands, sample) == 5
    same = {r["id"]: r.get("same_problem_as") for r in cands + sample}
    assert same == {"x-001": "x-D01", "x-002": "x-D01", "x-003": "x-D01", "x-004": "x-D01", "x-005": None, "x-006": None,
                    "x-007": None, "x-008": None, "x-009": "x-008", "x-D01": None, "x-D02": None, "x-D03": None}
    assert census.read_jsonl(tmp_path / "x" / "candidates.jsonl") == cands
    assert census.read_jsonl(tmp_path / "x" / "diverged_sample.jsonl") == sample


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
    progress = lambda read: {"candidates": {"total": {"candidates": 9, "read": read}},  # noqa: E731
                             "diverged": {"total": {"sampled": 4, "read": 0}}}
    s = {**progress(0), "flagged": {"total": 0, "distinct": {**d, "total": 0}}, "human_review": {}}
    out = readme.human_block(s, [], [])  # nothing read yet: both columns wait
    assert out.count("TODO") == 9 and "step 3 has not flagged any pair yet" in out
    s = {**progress(9), "flagged": {"total": 3, "distinct": d}, "human_review": {"reviewed": 0, "flagged": {"total": 3, "reviewed": 0}}}
    out = readme.human_block(s, [], [])
    assert out.count("TODO") == 5 and "0 of 3 flagged pairs" in out and "| LLM (step 3) | Human review |" in out
    rows = [{"id": "a-001", "impact": "runtime", "port": "verbatim"}, {"id": "a-002", "impact": "light", "port": "adapt"},
            {"id": "a-003", "impact": "runtime", "port": "verbatim", "same_problem_as": "a-001"}]
    final = [{"id": "a-001", "final": "missing"}, {"id": "a-002", "final": "not_missing"}, {"id": "a-003", "final": "missing"}]
    s["human_review"] = {"reviewed": 3, "final_missing_distinct": 1, "final": {"conflict": 0},
                         "flagged": {"total": 3, "reviewed": 3, "human_missing": 2}}
    out = readme.human_block(s, rows, final)
    assert "TODO" not in out and "in progress" not in out
    assert "| Fix–copy pairs that are missed fixes | 3 | 2 |" in out and "| Distinct problems | 2 | 1 |" in out
    assert "| Distinct problems that affect runtime behaviour | 1 | 1 |" in out and "2 of 3 reviewed" in out


def test_review_status_says_todo_until_review_starts():
    from fixprop import readme

    hs, hb = readme._human_status({})
    assert hs == "**TODO**, not started" and "TODO" in hb
    hs, hb = readme._human_status({"reviewed": 0, "flagged": {"total": 136, "reviewed": 0}})
    assert "TODO" in hs and "0 of 136" in hs and "TODO" in hb
    h = {"reviewed": 3, "final": {"missing": 2, "not_missing": 1, "unsure": 0, "conflict": 0}, "final_missing_distinct": 2,
         "flagged": {"total": 136, "reviewed": 3, "human_missing": 2, "human_not_missing": 1, "human_unsure": 0},
         "unflagged_reviewed": 0}
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
    write_labels(tiny, [
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
    assert it["group"] == "flagged" and it["model"]["label_text"] == "Flagged as a missed fix"
    assert it["model"]["title"] == "Line 30 was not carried over" and it["model"]["reader"] == "test"
    assert it["model"]["transcript"] == f"judgments/tiny/transcripts/{it['id']}.json.gz"
    (place,) = it["places"]
    lines = [t for h in place["fix"] for _, t in h["lines"]]
    assert "static int already_fixed_in_b_from_the_start(void);" in lines  # the fix: the new code
    assert len(lines) > 7  # more than 3 lines of context
    assert place["marks"] == [31] and any("driver_function_number_30" in ln for b in place["copy"] for ln in b["lines"])
    assert items[by["drv: fix line ten"]["id"]]["group"] == "other" and items[by["fix line fifty"]["id"]]["model"]["label"] is None
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
    assert h["flagged"] == {"total": 1, "reviewed": 1, "human_missing": 1, "human_not_missing": 0,
                            "human_unsure": 0, "human_conflict": 0}
    assert (h["unflagged_reviewed"], h["final"]["conflict"], h["final_missing_distinct"]) == (1, 1, 1)
    assert final[real]["model"] == {"label": "missing", "reader": "test", "same_problem_as": None}
    (d / "carol.jsonl").write_text(json.dumps({"id": real, "verdict": "maybe"}) + "\n")  # an invalid verdict is reported
    assert review.merge(found)[1] == 1


# ------------------------------------------------------------------ reading through the API


def _api_env(tiny, monkeypatch):
    """Run the pipeline on the small repository and point the reader at this one codebase."""
    from fixprop import labels, packets, reader

    _, _, _, recs, _ = run_steps(tiny)
    for mod in (packets, reader):
        monkeypatch.setattr(mod, "ORDER", ("tiny",))
    monkeypatch.setattr(packets, "get", lambda name: tiny)
    for mod in (reader, labels):
        monkeypatch.setattr(mod, "all_repos", lambda: dict(tiny.repos))
    return reader, {r["subject"]: r for r in recs}


def _model(good, bad=None, plain=False):
    """A stand-in for the API: it reads a file, submits bad once (if given), then submits good.
    With plain it only ever answers in text. Returns (the post function, the request bodies it received)."""
    seen = []

    def post(body):
        seen.append(json.loads(json.dumps(body)))
        n = sum(1 for m in body["messages"] if m["role"] == "assistant")
        usage = {"prompt_tokens": 100, "completion_tokens": 10, "cost": 0.01,
                 "prompt_tokens_details": {"cached_tokens": 50, "cache_write_tokens": 20},
                 "completion_tokens_details": {"reasoning_tokens": 4}}
        if plain:
            msg, why = {"role": "assistant", "content": "I think it is missing."}, "stop"
        else:
            if n == 0:
                name, args = "read_file", {"repo": "t/r", "path": "c/drv.c", "start_line": 30, "end_line": 32}
            else:
                name, args = "submit_judgment", (bad if n == 1 and bad else good)
            msg, why = {"role": "assistant", "content": None, "reasoning": f"step {n}", "tool_calls": [
                {"id": f"call_{n}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}, "tool_calls"
        return 200, {"id": f"gen-{n}", "model": "test/model", "provider": "Test",
                     "choices": [{"finish_reason": why, "message": msg}], "usage": usage}, "", None

    return post, seen


GOOD = {"label": "missing", "confidence": "high", "evidence": "c still has the old line 30.",
        "evidence_refs": [{"file": "c/drv.c", "line": 31, "quote": "driver_function_number_30"}], "port": "verbatim",
        "impact": "runtime", "where": {"file": "c/drv.c", "line": 31}, "title": "Line 30 was not carried over",
        "summary": "a fixed line 30; c still has the old line."}


def test_reader_tools_only_read_and_stay_inside_the_repositories(tiny, monkeypatch):
    reader, by = _api_env(tiny, monkeypatch)
    fix = by["drv: Fix line thirty"]["commit"]
    out = reader.run_tool("read_file", {"repo": "t/r", "path": "c/drv.c", "start_line": 31, "end_line": 31})
    assert out.split("\n") == ["t/r:c/drv.c at the pinned commit, lines 31-31 of 60",
                               "    31 static int driver_function_number_30(struct dev_s *dev);"]
    old = reader.run_tool("read_file", {"repo": "t/r", "path": "a/drv.c", "commit": fix + "^", "start_line": 31, "end_line": 31})
    assert "driver_function_number_30" in old and f"at commit {fix}^" in old  # a before its fix
    assert reader.run_tool("list_dir", {"repo": "t/r"}).split("\n")[1:] == ["a/", "b/", "c/"]
    found = reader.run_tool("search", {"repo": "t/r", "pattern": "already_fixed_in_b"})
    assert "a/drv.c:31:" in found and "b/drv.c:31:" in found and "c/drv.c" not in found
    assert "+static int already_fixed_in_b_from_the_start(void);" in reader.run_tool(
        "commit_diff", {"repo": "t/r", "commit": fix, "path": "a/drv.c"})
    assert "drv: Fix line thirty" in reader.run_tool("file_log", {"repo": "t/r", "path": "a/drv.c"})
    for name, args in (("read_file", {"repo": "t/r", "path": "../outside"}), ("read_file", {"repo": "t/r", "path": "/etc/passwd"}),
                       ("read_file", {"repo": "x/y", "path": "a/drv.c"}), ("list_dir", {"repo": "t/r", "path": ".git"}),
                       ("read_file", {"repo": "t/r", "path": "a/drv.c", "commit": "HEAD; id"}),
                       ("search", {"repo": "t/r", "pattern": "x", "path": "../.."}), ("run", {"cmd": "id"}),
                       ("read_file", "not an object")):
        assert reader.run_tool(name, args).startswith("Error:"), (name, args)
    names = [t["function"]["name"] for t in reader.tool_list("candidate")]
    assert names == ["read_file", "list_dir", "search", "commit_diff", "file_log", "submit_judgment"]
    assert reader.tool_list("diverged")[-1]["function"]["parameters"]["properties"]["label"]["enum"][1] == "same_bug"


def test_api_conversation_is_kept_in_full_and_becomes_a_judgment(tiny, tmp_path, monkeypatch):
    """One pair, one conversation: the first submission fails the check and is corrected; the transcript holds every
    request and response; the judgment built from it carries the transcript's checksum, which the merge verifies."""
    from fixprop import packets

    reader, by = _api_env(tiny, monkeypatch)
    sid = by["drv: Fix line thirty"]["id"]
    out = tmp_path / "judgments"
    bad = {**GOOD, "evidence_refs": [{"file": "c/drv.c", "line": 5, "quote": "driver_function_number_30"}]}
    post, seen = _model(GOOD, bad)
    assert reader.run(out, "test/model", provider=None, ids=[sid], jobs=1, post=post) == 0
    t = reader.load(out / "tiny" / "transcripts" / f"{sid}.json.gz")
    assert t["outcome"] == {"status": "accepted", "judgment": GOOD, "rejected_submissions": 1}
    assert t["usage"] == {"calls": 3, "prompt_tokens": 300, "cached_tokens": 150, "cache_write_tokens": 60,
                          "completion_tokens": 30, "reasoning_tokens": 12, "cost": 0.03}
    # the first request: the reading rules, then the packet, with the read-only tools and the settings
    assert seen[0]["messages"][0] == {"role": "system", "content": packets.GUIDE}
    assert seen[0]["messages"][1]["content"].startswith(f"ID: {sid}\n") and len(seen[0]["messages"]) == 2
    assert seen[0]["model"] == "test/model" and seen[0]["reasoning"] == {"effort": "medium"} and "provider" not in seen[0]
    # every later request is the earlier one plus what came back: the reply as received, then the tool result
    assert seen[1]["messages"][2]["reasoning"] == "step 0" and "driver_function_number_30" in seen[1]["messages"][3]["content"]
    assert seen[2]["messages"][-1]["content"].startswith("Not accepted.") and "c/drv.c:5" in seen[2]["messages"][-1]["content"]
    # and the transcript gives back every request exactly
    assert [c["messages_sent"] for c in t["calls"]] == [2, 4, 6] and [c["response"]["id"] for c in t["calls"]] == ["gen-0", "gen-1", "gen-2"]
    assert [{**t["request"], "messages": t["messages"][:c["messages_sent"]]} for c in t["calls"]] == seen
    assert t["messages"][-1] == {"role": "tool", "tool_call_id": "call_2", "content": "Accepted."}

    # judgments are built only when every pair has a transcript, unless asked otherwise
    assert reader.collect(out) > 0 and not (out / "tiny" / "labels.jsonl").exists()
    assert reader.collect(out, partial=True) == 0
    (row,) = census.read_jsonl(out / "tiny" / "labels.jsonl")
    assert (row["id"], row["label"], row["reader"]) == (sid, "missing", "test/model")
    assert row["transcript_sha256"] == reader.sha256(out / "tiny" / "transcripts" / f"{sid}.json.gz")
    # reading again skips it; the merge checks the transcript behind the judgment
    assert reader.run(out, "test/model", provider=None, ids=[sid], jobs=1, post=post) == 0 and len(seen) == 3
    monkeypatch.setattr(paths, "JUDGMENTS", out)
    _, _, _, recs, problems = run_steps(tiny)
    assert problems == 0 and next(r for r in recs if r["id"] == sid)["transcript"] == f"judgments/tiny/transcripts/{sid}.json.gz"
    reader.save(out / "tiny" / "transcripts" / f"{sid}.json.gz", {**t, "messages": t["messages"][:-1]})
    assert run_steps(tiny)[4] == 1


def test_api_run_keeps_failed_conversations_and_stops_on_a_bad_key(tiny, tmp_path, monkeypatch):
    reader, by = _api_env(tiny, monkeypatch)
    sid = by["drv: Fix line thirty"]["id"]
    out = tmp_path / "pilot"
    post, seen = _model(GOOD, plain=True)  # never calls a tool: reminded twice, then given up
    assert reader.run(out, "test/model", ids=[sid], jobs=1, post=post) == 1
    t = reader.load(out / "tiny" / "transcripts" / f"{sid}.failed-1.json.gz")
    assert t["outcome"]["status"] == "failed" and len(seen) == 3 and seen[1]["messages"][-1]["content"] == reader.NUDGE
    assert not (out / "tiny" / "transcripts" / f"{sid}.json.gz").exists()
    calls = []
    monkeypatch.setattr(reader, "_sleep", lambda s: None)

    def refused(body):
        calls.append(1)
        return 401, {"error": {"message": "No auth credentials found"}}, "", None

    assert reader.run(out, "test/model", jobs=1, post=refused) == 1 and len(calls) == 1  # one refusal stops the run
    monkeypatch.delenv(reader.KEY_VAR, raising=False)
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    assert reader.run(out, "test/model", ids=[sid]) == 2  # no key at all: nothing is sent


def test_api_request_settings_and_key():
    from fixprop import reader

    f = reader.fixed_request("candidate", "anthropic/claude-opus-5.5", "high", 1000, "anthropic")
    assert f["reasoning"] == {"effort": "high"} and f["max_tokens"] == 1000 and f["cache_control"] == {"type": "ephemeral"}
    assert f["provider"] == {"only": ["anthropic"], "allow_fallbacks": False, "require_parameters": True}
    assert "messages" not in f and len(f["tools"]) == 6
    g = reader.fixed_request("diverged", "other/model", None, 1000, None)
    assert set(g) == {"model", "tools", "max_tokens"}


def test_api_key_comes_from_the_environment_or_the_env_file(tmp_path, monkeypatch):
    from fixprop import reader

    monkeypatch.setattr(paths, "ROOT", tmp_path)
    for name in (reader.KEY_VAR, reader.URL_VAR):
        monkeypatch.delenv(name, raising=False)
    assert reader.api_key() is None and reader.endpoint() == "https://openrouter.ai/api/v1/chat/completions"
    (tmp_path / ".env").write_text('# a comment\nexport OPENROUTER_API_KEY="from-file"\nOPENROUTER_BASE_URL=http://localhost:1/v1/\n')
    assert reader.api_key() == "from-file" and reader.endpoint() == "http://localhost:1/v1/chat/completions"
    monkeypatch.setenv(reader.KEY_VAR, "from-environment")
    assert reader.api_key() == "from-environment"


def test_http_request_carries_the_key_and_the_body_and_nothing_about_the_sender(monkeypatch):
    import http.server
    import threading

    from fixprop import reader

    got = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            got.append(({k.lower(): v for k, v in self.headers.items()}, body))
            if len(got) == 1:  # the first attempt is told to wait
                self.send_response(429)
                self.send_header("Retry-After", "0")
                self.end_headers()
                self.wfile.write(b'{"error": {"message": "slow down"}}')
                return
            data = json.dumps({"choices": [{"message": {"role": "assistant", "content": "ok"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    waits = []
    monkeypatch.setattr(reader, "_sleep", waits.append)
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/chat/completions"
        body = {"model": "m", "messages": [{"role": "user", "content": "hello"}]}
        response, attempts = reader.complete(lambda b: reader.http_post(url, "sk-test-key", b), body)
    finally:
        server.shutdown()
    assert attempts == 2 and waits == [0.0] and response["choices"][0]["message"]["content"] == "ok"
    headers, sent = got[1]
    assert sent == body and headers["authorization"] == "Bearer sk-test-key" and headers["content-type"] == "application/json"
    assert set(headers) <= {"authorization", "content-type", "content-length", "host", "user-agent", "accept-encoding", "connection"}
    assert headers["user-agent"].startswith("Python-urllib/")  # the library's default, nothing of ours


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
    census.write_json(paths.LOCK, {"t/r": {"commit": sha, "cut_at": [], "commits": 7}})
    assert fetch.verify() == []
    census.write_json(paths.LOCK, {"t/r": {"commit": sha, "cut_at": [], "commits": 5}})
    assert any("the history differs from the record" in p for p in fetch.verify())
    census.write_json(paths.LOCK, {"t/r": {"commit": sha, "cut_at": [], "commits": 7}})
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
    for cmd in ("fetch", "all", "check", "review", "read"):
        assert cmd in out
    assert "packets" not in out
