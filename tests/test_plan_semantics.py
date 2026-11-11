"""Plan semantics (feat/plan-semantics): continuation / static op count, window match modes,
confirm_frames for OCR targets and text conditions, and the shape of a not condition.

FakeDeps from test_plan stands in for the screen; nothing here touches a window.
"""
from __future__ import annotations

import unittest

from cu_perceive import plan as plan_mod
from cu_perceive.plan import run_plan, static_ops, validate_plan, validate_report

from test_plan import PERCEIVE, FakeDeps, item

ACT = {"op": "act", "norm": "1,1"}
CAP = plan_mod.MAX_OPS


class WinDeps(FakeDeps):
    """FakeDeps plus top_windows (rows carry class) and a record of which lookup ran."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.lookups: list[str] = []

    def find_windows(self, title="", exe=""):
        self.lookups.append("find_windows")
        return super().find_windows(title, exe)

    def top_windows(self):
        self.lookups.append("top_windows")
        return list(self.windows)


def item_at(i, text, x, y):
    return {"id": i, "text": text, "norm": [x - 20, y - 10, x + 20, y + 10]}


class TestStaticCount(unittest.TestCase):
    def count(self, steps, **extra):
        plan, err = validate_plan({"resumable": True, "steps": steps, **extra})
        self.assertIsNone(err, err)
        return static_ops(plan)

    def test_straight_line_if_repeat_and_stop(self):
        self.assertEqual(self.count([PERCEIVE, ACT, ACT]), {"least": 3, "most": 3, "cap": CAP})
        c = self.count([PERCEIVE, {"op": "if", "cond": {"text": "x"}, "then": [ACT, ACT, ACT], "else": [ACT]}, ACT])
        self.assertEqual((c["least"], c["most"]), (1 + 2 + 1, 1 + 4 + 1))
        c = self.count([PERCEIVE, {"op": "repeat", "times": 5, "steps": [ACT, ACT]}])
        self.assertEqual((c["least"], c["most"]), (12, 12))
        c = self.count([PERCEIVE, {"op": "repeat", "times": 5, "until": {"text": "x"}, "steps": [ACT, ACT]}])
        self.assertEqual((c["least"], c["most"]), (2, 12))  # until may hold at once
        c = self.count([PERCEIVE, {"op": "stop"}, ACT, ACT])
        self.assertEqual((c["least"], c["most"]), (2, 2))  # nothing after an unconditional stop runs
        c = self.count([PERCEIVE, {"op": "if", "cond": {"text": "x"}, "then": [{"op": "stop"}]}, ACT, ACT])
        self.assertEqual((c["least"], c["most"]), (2, 5))  # a stop that may fire ends "least"
        c = self.count([{"op": "repeat", "times": 3, "steps": [ACT, {"op": "stop"}]}, ACT])
        self.assertEqual((c["least"], c["most"]), (3, 3))  # the body always stops: one round

    def test_validate_only_refuses_a_plan_that_surely_passes_the_cap(self):
        over = [PERCEIVE] + [ACT] * CAP  # 65 ops, every path
        rep = validate_report(over)
        self.assertEqual((rep["ok"], rep["reason"], rep["path"], rep["over_cap"]), (False, "bad_plan", "steps", True))
        self.assertEqual(rep["static_ops"], {"least": CAP + 1, "most": CAP + 1, "cap": CAP})
        self.assertIn("resumable", rep["hint"])
        self.assertEqual(validate_report([PERCEIVE] + [ACT] * (CAP - 1))["static_ops"]["least"], CAP)
        ok = validate_report({"resumable": True, "steps": over})
        self.assertTrue(ok["validated"])
        self.assertIn("continuation", ok["warnings"][0])

    def test_a_plan_that_may_pass_the_cap_validates_with_a_warning(self):
        rep = validate_report([PERCEIVE, {"op": "repeat", "times": 20, "until": {"text": "Done"}, "steps": [ACT] * 4}])
        self.assertTrue(rep["validated"])
        self.assertEqual(rep["static_ops"], {"least": 2, "most": 82, "cap": CAP})
        self.assertIn("up to 82 ops", rep["warnings"][0])
        self.assertNotIn("warnings", validate_report([PERCEIVE, ACT]))

    def test_resumable_and_resume_from_are_checked(self):
        def bad(plan, needle):
            _, err = validate_plan(plan)
            self.assertEqual(err["reason"], "bad_plan")
            self.assertIn(needle, err["error"])
        steps = [PERCEIVE, {"op": "if", "cond": {"text": "x"}, "then": [ACT]},
                 {"op": "repeat", "times": 3, "steps": [ACT, ACT]}]
        bad({"resumable": "yes", "steps": steps}, "resumable must be")
        bad({"resume_from": "9", "steps": steps}, "no op 9")
        bad({"resume_from": 1, "steps": steps}, "resume_from is an op path")
        bad({"resume_from": "1.else.0", "steps": steps}, "has no 'else'")
        bad({"resume_from": "2.3.0", "steps": steps}, "has no '3'")
        bad({"resume_from": "1.then", "steps": steps}, "names a branch")
        bad({"resume_from": "0.then.0", "steps": steps}, "has no 'then'")
        for ok in ("0", "1.then.0", "2.2.1", "2"):
            self.assertIsNone(validate_plan({"resume_from": ok, "steps": steps})[1], ok)
        # the static count follows the resume point: 64 acts left of 70 is within the cap
        plan, err = validate_plan({"resume_from": "6", "steps": [PERCEIVE] + [ACT] * 69})
        self.assertIsNone(err)
        self.assertEqual(static_ops(plan)["least"], CAP)
        plan, _ = validate_plan({"resume_from": "2.1.1", "steps": steps})
        self.assertEqual(static_ops(plan)["least"], 1 + 1 + 2)  # repeat row, rest of round 1, round 2

    def test_latest_may_be_seeded_from_stamps(self):
        self.assertIsNone(validate_plan({"stamps": {"latest": "s9.json"}, "steps": [ACT]})[1])


class TestContinuation(unittest.TestCase):
    def test_round_trip_carries_latest_and_named_stamps(self):
        steps = [PERCEIVE] + [ACT] * 69  # 70 ops
        deps = FakeDeps()
        first = run_plan({"resumable": True, "steps": steps}, go=True, deps=deps)
        self.assertEqual((first["ok"], first["stop_reason"], first["ops_run"]), (False, "max_ops", CAP))
        cont = first["continuation"]
        self.assertEqual(cont["resume_from"], str(CAP))
        self.assertEqual(cont["stamps"], {"main": "s0.json", "latest": "s0.json"})
        self.assertTrue(cont["resumable"])
        self.assertIn("merge continuation", first["hint"])
        self.assertEqual(len(deps.acts), CAP - 1)

        deps2 = FakeDeps()
        second = run_plan({"steps": steps, **cont}, go=True, deps=deps2)
        self.assertTrue(second["ok"], second)
        self.assertEqual((second["ops_run"], second["resumed_from"]), (70 - CAP, str(CAP)))
        self.assertEqual(second["results"][0]["path"], str(CAP))
        self.assertEqual(len(deps2.acts), 70 - CAP)
        self.assertEqual({c["stamp"] for _, c in deps2.acts}, {"s0.json"})  # the act with no stamp_ref used latest
        self.assertEqual(deps2.perceives, [])  # nothing re-perceived: the stamps came across
        self.assertNotIn("continuation", second)

    def test_carried_names_stay_one_frame(self):
        # a wait_for refreshes "latest"; "main" carried the same stamp, so it has to follow, as it would have
        # inside one call. Otherwise the act reads the old frame ("Old") and misses "Done".
        deps = FakeDeps(screens=[{"items": [item(1, "Done")]}])
        out = run_plan({"stamps": {"main": "a.json", "latest": "a.json"}, "steps": [
            {"op": "wait_for", "cond": {"text": "Done"}},
            {"op": "act", "stamp_ref": "main", "ocr_text": "Done"}]}, go=True, deps=deps)
        self.assertTrue(out["ok"], out)
        self.assertEqual(deps.acts[0][1]["stamp"], "s0.json")
        self.assertEqual(out["frames"]["main"]["json"], out["frames"]["latest"]["json"])

    def test_resume_inside_a_repeat_round(self):
        steps = [PERCEIVE, {"op": "repeat", "times": 20, "steps": [ACT] * 4}]  # 82 ops
        first = run_plan({"resumable": True, "steps": steps}, go=True, deps=FakeDeps())
        self.assertEqual(first["continuation"]["resume_from"], "1.15.2")
        deps = FakeDeps()
        second = run_plan({"steps": steps, **first["continuation"]}, go=True, deps=deps)
        self.assertTrue(second["ok"], second)
        row = second["results"][0]
        self.assertEqual((row["path"], row["resumed"], row["resumed_round"], row["iterations"]), ("1", True, 15, 20))
        self.assertEqual(second["results"][1]["path"], "1.15.2")
        self.assertEqual(len(deps.acts), 2 + 4 * 4)
        self.assertEqual(len(deps.acts) + CAP - 2, 80)  # every act of the plan ran exactly once over two calls

    def test_resume_inside_a_repeat_checks_until_before_the_next_round(self):
        steps = [PERCEIVE, {"op": "repeat", "times": 20, "until": {"text": "Done"}, "steps": [ACT] * 4}]
        deps = FakeDeps(screens=[{"items": [item(1, "Done")]}])
        out = run_plan({"resume_from": "1.15.2", "stamps": {"main": "m.json", "latest": "m.json"}, "steps": steps},
                       go=True, deps=deps)
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(deps.acts), 2)  # the rest of round 15, then until held
        self.assertTrue(out["results"][0]["until_met"])

    def test_resume_inside_an_if_branch_does_not_reevaluate(self):
        steps = [PERCEIVE, {"op": "if", "cond": {"text": "Go"}, "then": [ACT] * 70, "else": [{"op": "stop", "ok": False}]}]
        first = run_plan({"resumable": True, "steps": steps}, go=True,
                         deps=FakeDeps(screens=[{"items": [item(1, "Go")]}]))
        self.assertEqual(first["continuation"]["resume_from"], "1.then.62")
        deps = FakeDeps(screens=[{"items": []}])  # the condition would now take else
        second = run_plan({"steps": steps, **first["continuation"]}, go=True, deps=deps)
        self.assertTrue(second["ok"], second)
        self.assertEqual((second["results"][0]["branch"], second["results"][0]["resumed"]), ("then", True))
        self.assertNotIn("cond", second["results"][0])
        self.assertEqual(len(deps.acts), 70 - 62)

    def test_timeout_gives_a_continuation_and_abort_does_not(self):
        out = run_plan({"timeout_ms": 1000, "steps": [PERCEIVE, {"op": "wait", "sec": 5}, ACT]}, go=True,
                       deps=FakeDeps())
        self.assertEqual((out["stop_reason"], out["continuation"]["resume_from"]), ("timeout", "1"))
        deps = FakeDeps()
        deps.abort = True
        out = run_plan([PERCEIVE, ACT], go=True, deps=deps)
        self.assertEqual(out["stop_reason"], "aborted")
        self.assertNotIn("continuation", out)

    def test_dry_run_continuation_says_so(self):
        out = run_plan({"resumable": True, "steps": [PERCEIVE] + [ACT] * 69}, deps=FakeDeps())
        self.assertEqual(out["stop_reason"], "max_ops")
        self.assertIn("dry run", out["hint"])
        self.assertEqual(set(out["continuation"]), {"resume_from", "stamps", "resumable"})

    def test_a_last_condition_starts_empty_after_resume(self):
        out = run_plan({"resume_from": "1", "stamps": {"latest": "a.json"}, "steps": [
            ACT, {"op": "assert", "cond": {"last": {"ok": True}}}]}, go=True, deps=FakeDeps())
        self.assertEqual(out["stop_reason"], "assert_failed")


class TestWindowMatch(unittest.TestCase):
    WINS = [{"hwnd": 1, "title": "Hacker News", "exe": "chrome.exe", "class": "Chrome_WidgetWin_1"},
            {"hwnd": 2, "title": "Show HN: a thing | Hacker News", "exe": "chrome.exe", "class": "Chrome_WidgetWin_1"},
            {"hwnd": 3, "title": "Notepad", "exe": "notepad.exe", "class": "Notepad"}]

    def check(self, cond, wins=None):
        deps = WinDeps(windows=wins if wins is not None else self.WINS)
        out = run_plan([{"op": "assert", "cond": cond, "on_fail": "continue"}], go=True, deps=deps)
        return out["results"][0]["cond"], deps

    def test_contains_is_the_default_and_unchanged(self):
        ev, deps = self.check({"window": {"title": "hacker news"}})
        self.assertEqual((ev["met"], ev["count"], ev["match"]), (True, 2, "contains"))
        self.assertEqual(deps.lookups, ["find_windows"])  # same lookup as before

    def test_exact_and_left_this_page(self):
        ev, _ = self.check({"window": {"title": "hacker news", "match": "exact"}})
        self.assertEqual((ev["met"], ev["count"], ev["first"]["hwnd"]), (True, 1, 1))
        # "left the front page" while only the article is open: contains says no forever, exact says yes
        article = [w for w in self.WINS if w["hwnd"] != 1]
        ev, _ = self.check({"not": {"window": {"title": "Hacker News"}}}, article)
        self.assertFalse(ev["met"])
        ev, _ = self.check({"not": {"window": {"title": "Hacker News", "match": "exact"}}}, article)
        self.assertTrue(ev["met"])
        ev, _ = self.check({"not": {"window": {"title": "Hacker News"}, "match": "exact"}}, article)
        self.assertTrue(ev["met"])  # match beside the window body works the same

    def test_regex_and_class(self):
        ev, _ = self.check({"window": {"title": r"^show hn:.*\| hacker news$", "match": "regex"}})
        self.assertEqual((ev["met"], ev["first"]["hwnd"]), (True, 2))
        ev, _ = self.check({"window": {"class": "notepad", "match": "exact"}})
        self.assertEqual((ev["count"], ev["first"]["class"]), (1, "Notepad"))
        ev, deps = self.check({"window": {"class": "WidgetWin"}})
        self.assertEqual((ev["count"], deps.lookups), (2, ["top_windows"]))
        ev, _ = self.check({"window": {"exe": "chrome", "title": "Hacker News", "match": "exact"}})
        self.assertEqual(ev["count"], 1)
        ev, _ = self.check({"window": {"exe": "notepad", "title": "Hacker News", "match": "exact"}})
        self.assertFalse(ev["met"])

    def test_refusals(self):
        def bad(cond, needle):
            _, err = validate_plan([{"op": "assert", "cond": cond}])
            self.assertIsNotNone(err, cond)
            self.assertIn(needle, err["error"])
        bad({"window": {"title": "a", "match": "glob"}}, "contains, exact or regex")
        bad({"window": {"title": "(", "match": "regex"}}, "not a valid regex")
        bad({"window": {"title": "a" * 300, "match": "regex"}}, "longer than")
        bad({"window": {"title": "a", "match": "exact"}, "match": "regex"}, "given twice")
        bad({"window": {"minimized": True}}, "title, exe and/or class")
        bad({"window": {"title": 3}}, "must be a string")
        bad({"window": {"title": "a", "minimized": "no"}}, "minimized must be")


class TestOcrConfirm(unittest.TestCase):
    def test_off_by_default_one_perceive(self):
        deps = FakeDeps(screens=[{"items": [item(1, "Save")]}])
        out = run_plan([PERCEIVE, {"op": "act", "ocr_text": "Save"}], go=True, deps=deps)
        self.assertTrue(out["ok"])
        self.assertEqual(len(deps.perceives), 1)
        self.assertNotIn("confirm_frames", out["results"][1])

    def test_stable_match_acts_on_the_confirming_frame(self):
        deps = FakeDeps(screens=[{"items": [item_at(4, "299 comments", 300, 200)]},
                                 {"items": [item_at(7, "299 comments", 305, 204)]}])
        out = run_plan([PERCEIVE, {"op": "act", "ocr_text": "299 comments", "confirm_frames": 2}], go=True, deps=deps)
        self.assertTrue(out["ok"], out)
        steps, call = deps.acts[0]
        self.assertEqual((steps[0]["ids"], call["stamp"]), ("7", "s1.json"))
        self.assertNotIn("confirm_frames", steps[0])
        self.assertEqual([f["stamp"] for f in out["results"][1]["confirm_frames"]], ["s0", "s1"])
        self.assertEqual(deps.perceives[1], {"hwnd": 100, "ocr": True})  # same window, same detectors

    def test_a_moved_or_vanished_match_fails_closed(self):
        deps = FakeDeps(screens=[{"items": [item_at(4, "299 comments", 300, 200)]},
                                 {"items": [item_at(4, "299 comments", 300, 600)]}])
        out = run_plan([PERCEIVE, {"op": "act", "ocr_text": "299 comments", "confirm_frames": 2}], go=True, deps=deps)
        row = out["results"][1]
        self.assertEqual((out["stop_reason"], row["step"]), ("unstable_target", 0))
        self.assertEqual([f["stamp"] for f in row["frames"]], ["s0", "s1"])
        self.assertEqual(deps.acts, [])
        deps = FakeDeps(screens=[{"items": [item_at(4, "299 comments", 300, 200)]}, {"items": [item(1, "Other")]}])
        out = run_plan([PERCEIVE, {"op": "act", "ocr_text": "299 comments", "confirm_frames": 2}], go=True, deps=deps)
        self.assertEqual((out["stop_reason"], out["results"][1]["frame"]), ("target_not_found", 2))
        self.assertEqual(deps.acts, [])

    def test_dry_run_confirms_too_and_sends_nothing(self):
        deps = FakeDeps(screens=[{"items": [item(1, "Save")]}])
        out = run_plan([PERCEIVE, {"op": "act", "steps": [{"ocr_text": "Save", "confirm_frames": 2}, {"key": "enter"}]}],
                       deps=deps)
        self.assertTrue(out["ok"], out)
        self.assertEqual((len(deps.perceives), deps.acts[0][1]["go"]), (2, False))

    def test_refusals(self):
        def bad(op, needle):
            _, err = validate_plan([PERCEIVE, op])
            self.assertIsNotNone(err, op)
            self.assertIn(needle, err["error"])
        bad({"op": "act", "norm": "1,1", "confirm_frames": 2}, "confirm_frames goes with ocr_text")
        bad({"op": "act", "ocr_text": "a", "confirm_frames": 9}, "confirm_frames must be")
        bad({"op": "act", "ocr_text": "a", "confirm_frames": True}, "confirm_frames must be")
        bad({"op": "assert", "cond": {"text": "a", "confirm_frames": 0}}, "confirm_frames must be")

    def test_reasons_keep_candidates_and_similar_texts(self):
        deps = FakeDeps(screens=[{"items": [item(1, "Submit"), item(2, "Submit order"), item(3, "Cancel")]}])
        out = run_plan([PERCEIVE, {"op": "act", "ocr_text": "Subm1t"}], go=True, deps=deps)
        row = out["results"][1]
        self.assertEqual((row["reason"], row["match_count"], row["ocr_items"], row["candidates"]),
                         ("target_not_found", 0, 3, []))
        self.assertEqual(row["similar"][0]["text"], "Submit")
        self.assertEqual(row["error"], "ocr_text 'Subm1t' matches 0 items on stamp s0")
        out = run_plan([PERCEIVE, {"op": "act", "ocr_text": "ubmi"}], go=True,
                       deps=FakeDeps(screens=deps.screens))
        row = out["results"][1]
        self.assertEqual((row["reason"], row["matched_by"], [c["id"] for c in row["candidates"]]),
                         ("ambiguous_target", "contains", [1, 2]))

    def test_text_condition_confirm(self):
        steady = FakeDeps(screens=[{"items": [item(1, "Done")]}])
        out = run_plan([PERCEIVE, {"op": "assert", "cond": {"text": "Done", "confirm_frames": 2}}], go=True, deps=steady)
        self.assertTrue(out["ok"], out)
        self.assertEqual([f["stamp"] for f in out["results"][1]["cond"]["confirm_frames"]], ["s0", "s1"])
        flicker = FakeDeps(screens=[{"items": [item(1, "Done")]}, {"items": []}])
        out = run_plan([PERCEIVE, {"op": "assert", "cond": {"text": "Done", "confirm_frames": 2}}], go=True,
                       deps=flicker)
        cond = out["results"][1]["cond"]
        self.assertEqual((out["stop_reason"], cond["met"], cond["unstable"]), ("assert_failed", False, True))
        miss = FakeDeps(screens=[{"items": []}])
        run_plan([PERCEIVE, {"op": "assert", "cond": {"text": "Done", "confirm_frames": 2}}], go=True, deps=miss)
        self.assertEqual(len(miss.perceives), 1)  # no match on the first frame: nothing to confirm

    def test_wait_for_with_confirm_needs_two_frames_in_a_row(self):
        deps = FakeDeps(screens=[{"items": []}, {"items": [item(1, "Done")]}, {"items": []},
                                 {"items": [item(1, "Done")]}, {"items": [item(1, "Done")]}])
        out = run_plan([PERCEIVE, {"op": "wait_for", "cond": {"text": "Done", "confirm_frames": 2}}], go=True, deps=deps)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["results"][1]["polls"], 2)


class TestNotShape(unittest.TestCase):
    def test_not_reports_inner_met_and_the_negation(self):
        deps = FakeDeps(windows=[{"hwnd": 1, "title": "App", "exe": "app.exe"}])
        out = run_plan([{"op": "assert", "cond": {"not": {"window": {"title": "Error"}}}}], go=True, deps=deps)
        cond = out["results"][0]["cond"]
        self.assertTrue(out["ok"])
        self.assertEqual((cond["met"], cond["not"]["met"], cond["not"]["inner_met"]), (True, True, False))
        self.assertEqual(cond["not"]["count"], 0)  # the inner evidence is kept beside them
        out = run_plan([{"op": "assert", "cond": {"not": {"not": {"window": {"title": "App"}}}}}], go=True, deps=deps)
        cond = out["results"][0]["cond"]
        self.assertEqual((cond["met"], cond["not"]["inner_met"], cond["not"]["not"]["inner_met"]), (True, False, True))


class TestExamples(unittest.TestCase):
    def test_current_examples_validate(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "docs" / "examples"
        for name in ("plan-wait-and-branch.json", "plan-scroll-until.json", "plan-browser-read-body.json",
                     "plan-browser-challenge-trap.json", "plan-left-page-confirm.json"):
            rep = validate_report(str(root / name))
            self.assertTrue(rep.get("validated"), (name, rep))


if __name__ == "__main__":
    unittest.main()
