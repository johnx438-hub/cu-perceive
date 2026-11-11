"""ComputerPlan (cu_perceive.plan): validation, dry-run semantics, conditions, bounds.

The runner's hands and eyes are replaced by FakeDeps, so nothing here touches a window.
The last class runs the real Deps on an image stamp (dry run only).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cu_perceive import plan as plan_mod
from cu_perceive.plan import Deps, run_plan, validate_plan


def item(i, text):
    return {"id": i, "text": text, "norm": [100, 100 * i, 200, 100 * i + 40]}


class FakeDeps(Deps):
    """screens: what each successive perceive sees (the last one repeats)."""

    def __init__(self, screens=None, windows=None, fg=None):
        self.screens = list(screens or [{"items": []}])
        self.windows = list(windows or [])
        self.fg = fg or {"foreground_hwnd": 100, "title": "App"}
        self.now = 0.0
        self.perceives: list[dict] = []
        self.acts: list[tuple[list, dict]] = []
        self.launched: list = []
        self.act_results: list[dict] = []
        self.abort = False
        self.on_act = None

    def clock(self):
        return self.now

    def sleep(self, s):
        self.now += s

    def perceive(self, **kw):
        if kw.get("title") == "missing":
            raise RuntimeError("no window title='missing' exe=None")
        n = len(self.perceives)
        self.perceives.append(kw)
        screen = self.screens[min(n, len(self.screens) - 1)]
        return {"stamp": f"s{n}", "json": f"s{n}.json", "map": f"s{n}.grid.png", "hwnd": kw.get("hwnd") or 100,
                "width": 800, "height": 600, "ok": True, "ocr": bool(kw.get("ocr")), "uia": bool(kw.get("uia")),
                "items": screen.get("items", []), "uia_nodes": screen.get("uia_nodes", [])}

    def read_frame(self, stamp):
        return {"stamp": stamp, "hwnd": 100, "ocr": True, "items": [item(1, "Old")], "uia_nodes": []}

    def act(self, steps, call):
        self.acts.append((steps, call))
        if self.on_act:
            self.on_act(self)
        if self.act_results:
            return self.act_results.pop(0)
        go = bool(call.get("go"))
        return {"ok": True, "dry_run": not go, "step_count": len(steps), "verification": "sent" if go else None,
                "evidence": {"steps_sent": len(steps)} if go else {}, "completed_steps": []}

    def find_windows(self, title="", exe=""):
        return [w for w in self.windows if title.lower() in w["title"].lower() and exe.lower() in w.get("exe", "")]

    def foreground(self):
        return self.fg

    def launch(self, app=None, exe=None):
        self.launched.append(app or exe)
        return {"ok": True, "exe": exe or app, "window": {"hwnd": 7, "title": "New"}}

    def abort_state(self):
        return {"abort_active": self.abort}


PERCEIVE = {"op": "perceive", "hwnd": 100, "ocr": True, "as": "main"}


class TestValidate(unittest.TestCase):
    def bad(self, plan, needle):
        ok, err = validate_plan(plan)
        self.assertIsNone(ok)
        self.assertEqual(err["reason"], "bad_plan")
        self.assertIn(needle, err["error"])
        return err

    def test_refusals(self):
        self.bad({"go": True, "steps": [PERCEIVE]}, "go is not a plan field")
        self.bad({"steps": []}, "non-empty list")
        self.bad([{"op": "click"}], "each op is an object")
        self.bad([{"op": "wait", "sec": 1, "ms": 5}], "unknown wait op field")
        self.bad([{"op": "wait", "sec": 99}], "sec in")
        self.bad([{"op": "perceive"}], "needs hwnd, title, exe or from")
        self.bad([{"op": "perceive", "hwnd": 1, "as": "latest"}], "as must be")
        self.bad([{"op": "act", "stamp_ref": "nope", "norm": "1,1"}], "'nope' is not defined")
        self.bad([PERCEIVE, {"op": "act", "stamp": "x", "stamp_ref": "main", "norm": "1,1"}], "not both")
        self.bad([PERCEIVE, {"op": "act", "key": "enter", "steps": [{"norm": "1,1"}]}], "cannot be combined")
        self.bad([PERCEIVE, {"op": "act", "ocr_text": "OK", "norm": "1,1"}], "ocr_text cannot be combined")
        self.bad([PERCEIVE, {"op": "act", "steps": [{"norm": "1,1", "expect_change": True}, {"key": "tab"}]}],
                 "not the last step")
        self.bad([PERCEIVE, {"op": "act", "steps": [{"x": 1}]}], "unknown field")
        self.bad([{"op": "repeat", "times": 21, "steps": [{"op": "wait", "sec": 1}]}], "times in 1..20")
        self.bad([{"op": "if", "cond": {"window": {"title": "a"}}}], "then and/or else")
        self.bad([{"op": "assert", "cond": {"text": "a", "window": {"title": "b"}}}], "exactly one of")
        self.bad([{"op": "assert", "cond": {"text": "a", "match": "regex"}}], "match is")
        self.bad([{"op": "assert", "cond": {"eval": "1+1"}}], "exactly one of")
        self.bad([{"op": "launch", "app": "a", "exe": "b"}], "exactly one of app, exe")
        self.bad({"version": 2, "steps": [PERCEIVE]}, "unsupported version")
        self.bad({"timeout_ms": 10 ** 9, "steps": [PERCEIVE]}, "timeout_ms")
        self.bad("{not json", "not valid JSON")

    def test_error_names_the_op(self):
        err = self.bad([PERCEIVE, {"op": "if", "cond": {"last": {"ok": True}}, "then": [{"op": "wait"}]}], "sec in")
        self.assertEqual(err["path"], "1.then.0")

    def test_nesting_cap(self):
        ops = [{"op": "wait", "sec": 1}]
        for _ in range(plan_mod.MAX_DEPTH):
            ops = [{"op": "repeat", "times": 1, "steps": ops}]
        self.bad(ops, "nest deeper")

    def test_accepts_list_json_text_and_file(self):
        steps = [PERCEIVE, {"op": "act", "stamp_ref": "main", "norm": "500,500", "text": "hi", "replace": True}]
        for src in (steps, {"version": 1, "goal": "g", "steps": steps}, json.dumps(steps)):
            ok, err = validate_plan(src)
            self.assertIsNone(err, err)
            self.assertEqual(ok["steps"], steps)
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "p.json"
            f.write_text(json.dumps({"steps": steps}), encoding="utf-8")
            self.assertIsNone(validate_plan(str(f))[1])
        self.assertIn("cannot read plan file", validate_plan("no-such-plan.json")[1]["error"])

    def test_bad_plan_runs_nothing(self):
        deps = FakeDeps()
        out = run_plan([PERCEIVE, {"op": "wait"}], go=True, deps=deps)
        self.assertFalse(out["ok"])
        self.assertFalse(out["dry_run"])
        self.assertEqual(deps.perceives, [])


class TestDryRun(unittest.TestCase):
    def test_perceives_plans_and_marks_later_checks_speculative(self):
        deps = FakeDeps(screens=[{"items": [item(1, "Name")]}])
        out = run_plan({"goal": "fill", "steps": [
            PERCEIVE,
            {"op": "assert", "cond": {"text": "Name"}},
            {"op": "act", "norm": "500,500", "text": "x"},
            {"op": "wait", "sec": 2},
            {"op": "wait_for", "cond": {"text": "Saved"}, "timeout_ms": 3000},
            {"op": "assert", "cond": {"text": "Saved", "fresh": True}},
            {"op": "launch", "app": "calc"},
        ]}, deps=deps)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["dry_run"])
        self.assertIsNone(out["verification"])
        self.assertEqual(out["planned_acts"], 2)
        self.assertEqual(deps.now, 0.0)  # no waiting
        self.assertEqual(deps.launched, [])
        self.assertFalse(deps.acts[0][1]["go"])
        self.assertEqual(deps.acts[0][1]["stamp"], "s0.json")
        rows = out["results"]
        self.assertTrue(rows[1]["ok"])
        self.assertNotIn("speculative", rows[1]["cond"])
        self.assertEqual(rows[3]["would_wait_sec"], 2)
        self.assertIsNone(rows[4]["ok"])
        self.assertEqual(rows[4]["polls"], 1)
        self.assertTrue(rows[4]["cond"]["speculative"])
        self.assertIsNone(rows[5]["ok"])
        self.assertEqual(rows[5]["would_fail_now"], "assert_failed")
        self.assertEqual(rows[6]["would_launch"], "calc")

    def test_check_before_any_act_is_real(self):
        out = run_plan([PERCEIVE, {"op": "assert", "cond": {"text": "Nope"}, "message": "wrong page"},
                        {"op": "act", "norm": "1,1"}], deps=(deps := FakeDeps()))
        self.assertFalse(out["ok"])
        self.assertEqual((out["stopped_at"], out["stop_reason"], out["error"]), ("1", "assert_failed", "wrong page"))
        self.assertEqual(deps.acts, [])

    def test_repeat_plans_one_round(self):
        deps = FakeDeps()
        out = run_plan([PERCEIVE, {"op": "repeat", "times": 5, "until": {"text": "End"},
                                   "steps": [{"op": "act", "norm": "500,500", "scroll": "down:5"}]}], deps=deps)
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(deps.acts), 1)
        self.assertEqual(out["results"][1]["would_repeat_up_to"], 5)


class TestLive(unittest.TestCase):
    def test_wait_for_text_then_click_it_by_ocr_text(self):
        deps = FakeDeps(screens=[{"items": [item(1, "Start")]}, {"items": []}, {"items": []},
                                 {"items": [item(4, "Done"), item(5, "Open folder")]}])
        out = run_plan([
            PERCEIVE,
            {"op": "act", "ocr_text": "start", "check_change": True},
            {"op": "wait_for", "cond": {"text": "Done"}, "timeout_ms": 5000, "interval_ms": 500},
            {"op": "act", "steps": [{"ocr_text": "Open", "expect_change": True}]},
        ], go=True, deps=deps)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["verification"], "sent")
        self.assertEqual(out["evidence"]["steps_sent"], 2)
        self.assertEqual(deps.acts[0][0], [{"ids": "1"}])
        self.assertTrue(deps.acts[0][1]["check_change"])
        self.assertTrue(deps.acts[0][1]["go"])
        wait = out["results"][2]
        self.assertEqual((wait["met"], wait["polls"]), (True, 3))
        self.assertEqual(deps.now, 1.0)
        # the act after the wait runs on the frame the wait saw, re-perceived with the same detectors
        self.assertEqual(deps.acts[1][0], [{"expect_change": True, "ids": "5"}])
        self.assertEqual(deps.acts[1][1]["stamp"], "s3.json")
        self.assertEqual(deps.perceives[3], {"hwnd": 100, "ocr": True})
        self.assertEqual(out["frames"]["main"]["stamp"], "s3")

    def test_wait_timeout_stops_and_reports_partial(self):
        deps = FakeDeps()
        out = run_plan([PERCEIVE, {"op": "act", "norm": "1,1"},
                        {"op": "wait_for", "cond": {"window": {"title": "Save As"}}, "timeout_ms": 1000},
                        {"op": "act", "key": "enter"}], go=True, deps=deps)
        self.assertFalse(out["ok"])
        self.assertEqual((out["stopped_at"], out["stop_reason"], out["verification"]), ("2", "wait_timeout", "partial"))
        self.assertEqual(len(deps.acts), 1)
        self.assertIn("hint", out)

    def test_if_else_and_window_condition(self):
        plan = [PERCEIVE, {"op": "if", "cond": {"window": {"title": "Login"}},
                           "then": [{"op": "act", "key": "esc"}],
                           "else": [{"op": "stop", "message": "already in"}]},
                {"op": "act", "norm": "9,9"}]
        deps = FakeDeps(windows=[{"hwnd": 5, "title": "Login - App", "exe": "app.exe"}])
        out = run_plan(plan, go=True, deps=deps)
        self.assertTrue(out["ok"])
        self.assertEqual([r["path"] for r in out["results"]], ["0", "1", "1.then.0", "2"])
        self.assertEqual(out["results"][1]["cond"]["first"]["hwnd"], 5)
        deps = FakeDeps()
        out = run_plan(plan, go=True, deps=deps)
        self.assertTrue(out["ok"])
        self.assertEqual((out["stopped"], out["stop_reason"], out["message"]), (True, "stop_op", "already in"))
        self.assertEqual(out["verification"], "no_input")
        self.assertEqual(deps.acts, [])

    def test_on_fail_continue_then_branch_on_last(self):
        deps = FakeDeps()
        deps.act_results = [{"ok": False, "reason": "point_covered", "error": "covered", "dry_run": False,
                             "evidence": {"steps_sent": 0}, "suggested_hwnd": 77,
                             "completed_steps": [{"index": 0, "ok": False}]}]
        out = run_plan([PERCEIVE,
                        {"op": "act", "norm": "1,1", "on_fail": "continue"},
                        {"op": "if", "cond": {"last": {"ok": False, "reason": "point_covered"}},
                         "then": [{"op": "perceive", "hwnd": 77, "as": "popup"},
                                  {"op": "act", "stamp_ref": "popup", "key": "esc"}]}], go=True, deps=deps)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["results"][1]["continued"])
        self.assertEqual(out["results"][1]["failed_step"], {"index": 0, "ok": False})
        self.assertEqual(out["results"][2]["cond"]["last"], {"ok": False, "reason": "point_covered"})
        self.assertEqual(deps.acts[1][1]["stamp"], "s1.json")

    def test_act_failure_stops_by_default(self):
        deps = FakeDeps()
        deps.act_results = [{"ok": False, "reason": "typed_mismatch", "error": "mismatch", "dry_run": False,
                             "evidence": {"steps_sent": 1}}]
        out = run_plan([PERCEIVE, {"op": "act", "norm": "1,1", "text": "a"}, {"op": "act", "key": "enter"}],
                       go=True, deps=deps)
        self.assertEqual((out["ok"], out["stop_reason"], out["verification"]), (False, "typed_mismatch", "partial"))
        self.assertEqual(len(deps.acts), 1)

    def test_repeat_until_found_and_exhausted(self):
        screens = [{"items": []}, {"items": []}, {"items": [item(3, "Target row")]}]
        plan = [PERCEIVE, {"op": "repeat", "times": 4, "until": {"text": "Target"},
                           "steps": [{"op": "act", "norm": "500,500", "scroll": "down:5"}, {"op": "wait", "sec": 0.5}]},
                {"op": "act", "ocr_text": "Target row"}]
        deps = FakeDeps(screens=screens)
        out = run_plan(plan, go=True, deps=deps)
        self.assertTrue(out["ok"], out)
        rep = out["results"][1]
        self.assertEqual((rep["iterations"], rep["until_met"]), (1, True))
        self.assertEqual(deps.acts[-1][0], [{"ids": "3"}])
        deps = FakeDeps()
        out = run_plan(plan, go=True, deps=deps)
        self.assertEqual((out["ok"], out["stop_reason"]), (False, "repeat_exhausted"))
        self.assertEqual(len(deps.acts), 4)

    def test_ocr_text_must_be_unique(self):
        deps = FakeDeps(screens=[{"items": [item(1, "Save"), item(2, "Save as"), item(3, "save")]}])
        out = run_plan([PERCEIVE, {"op": "act", "ocr_text": "Save"}], go=True, deps=deps)
        self.assertEqual(out["stop_reason"], "ambiguous_target")  # two exact matches (case-insensitive)
        self.assertEqual([c["id"] for c in out["results"][1]["candidates"]], [1, 3])
        self.assertEqual(deps.acts, [])
        out = run_plan([PERCEIVE, {"op": "act", "ocr_text": "as"}], go=True, deps=FakeDeps(screens=deps.screens))
        self.assertTrue(out["ok"])  # no exact match, one contains match
        out = run_plan([PERCEIVE, {"op": "act", "ocr_text": "Quit"}], go=True, deps=FakeDeps(screens=deps.screens))
        self.assertEqual(out["stop_reason"], "target_not_found")

    def test_text_needs_a_text_source_and_never_guesses(self):
        out = run_plan([{"op": "perceive", "hwnd": 100}, {"op": "if", "cond": {"not": {"text": "x"}},
                                                         "then": [{"op": "act", "norm": "1,1"}]}],
                       go=True, deps=(deps := FakeDeps()))
        self.assertEqual((out["ok"], out["stop_reason"]), (False, "no_text_source"))
        self.assertEqual(deps.acts, [])

    def test_uia_foreground_and_combinators(self):
        deps = FakeDeps(screens=[{"uia_nodes": [{"id": 1, "name": "Agree", "control_type": "CheckBoxControl",
                                                 "value": "off"}]}], fg={"foreground_hwnd": 100, "title": "Setup"})
        cond = {"all": [{"uia": {"name": "agree", "control_type": "checkboxcontrol", "value": "off"}},
                        {"any": [{"foreground": {"title": "nope"}}, {"foreground": {"stamp_ref": "main"}}]},
                        {"not": {"window": {"title": "Error"}}}]}
        out = run_plan([{"op": "perceive", "hwnd": 100, "uia": True, "as": "main"}, {"op": "assert", "cond": cond}],
                       go=True, deps=deps)
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(deps.perceives), 1)  # not fresh: the stamp is read as it is

    def test_stamps_map_and_from(self):
        deps = FakeDeps(screens=[{"items": [item(2, "New")]}])
        out = run_plan({"stamps": {"main": "20260101-000000-000"}, "steps": [
            {"op": "act", "stamp_ref": "main", "ocr_text": "Old"},
            {"op": "perceive", "from": "main"},
            {"op": "act", "stamp_ref": "main", "ocr_text": "New"},
        ]}, go=True, deps=deps)
        self.assertTrue(out["ok"], out)
        self.assertEqual(deps.acts[0][1]["stamp"], "20260101-000000-000")
        self.assertEqual(deps.perceives, [{"hwnd": 100, "ocr": True}])
        self.assertEqual(deps.acts[1][1]["stamp"], "s0.json")

    def test_perceive_failure(self):
        out = run_plan([{"op": "perceive", "title": "missing"}, {"op": "act", "norm": "1,1"}], go=True, deps=FakeDeps())
        self.assertEqual((out["ok"], out["stop_reason"], out["verification"]), (False, "perceive_failed", "refused"))
        self.assertIn("no window", out["error"])
        out = run_plan([{"op": "act", "norm": "1,1"}], go=True, deps=FakeDeps())
        self.assertEqual(out["stop_reason"], "no_stamp")

    def test_abort_stops_before_the_next_op(self):
        deps = FakeDeps()
        deps.on_act = lambda d: setattr(d, "abort", True)
        out = run_plan([PERCEIVE, {"op": "act", "norm": "1,1"}, {"op": "act", "norm": "2,2"}], go=True, deps=deps)
        self.assertEqual((out["ok"], out["stopped_at"], out["verification"]), (False, "2", "aborted"))
        self.assertTrue(out["aborted"])
        self.assertEqual(len(deps.acts), 1)
        self.assertIn("do not retry", out["hint"])
        deps = FakeDeps()
        deps.abort = True
        self.assertTrue(run_plan([PERCEIVE, {"op": "act", "norm": "1,1"}], deps=deps)["ok"])  # dry runs still plan

    def test_gated_op_gets_a_result_row(self):
        """A refused op leaves a row, so stopped_at always names an entry in results."""
        deps = FakeDeps()
        deps.on_act = lambda d: setattr(d, "abort", True)
        out = run_plan([PERCEIVE, {"op": "act", "norm": "1,1"}, {"op": "act", "norm": "2,2", "note": "n"}],
                       go=True, deps=deps)
        self.assertEqual([r["path"] for r in out["results"]], ["0", "1", "2"])
        row = out["results"][2]
        self.assertEqual((row["op"], row["gated"], row["ran"], row["ok"], row["reason"], row["note"]),
                         ("act", True, False, False, "aborted", "n"))
        self.assertEqual((out["stopped_at"], out["stop_reason"]), ("2", "aborted"))

    def test_dry_run_lists_the_ops_that_would_fail(self):
        """A typoed ocr_text (or stale stamp_ref) cannot hide behind ok: true."""
        deps = FakeDeps(screens=[{"items": [item(1, "Save")]}])
        out = run_plan([PERCEIVE,
                        {"op": "act", "norm": "500,500", "text": "x"},
                        {"op": "act", "ocr_text": "Quit", "on_fail": "continue"}], deps=deps)
        self.assertTrue(out["ok"], out)
        row = out["results"][2]
        self.assertEqual((row["ok"], row["reason"], row["would_fail_now"], row["speculative"]),
                         (None, "target_not_found", "target_not_found", True))
        self.assertEqual(out["would_fail"], [{"path": "2", "op": "act", "reason": "target_not_found",
                                              "error": "ocr_text 'Quit' matches 0 items on stamp s0"}])
        self.assertEqual(out["would_fail_count"], 1)
        self.assertIn("1 op(s) would fail", out["warnings"][0])

    def test_dry_run_without_failures_has_no_would_fail_list(self):
        out = run_plan([PERCEIVE, {"op": "act", "norm": "1,1"},
                        {"op": "assert", "cond": {"text": "Save"}}], deps=FakeDeps(
            screens=[{"items": [item(1, "Save")]}]))
        self.assertNotIn("would_fail", out)
        self.assertNotIn("warnings", out)

    def test_wait_reports_what_it_slept_when_aborted(self):
        deps = FakeDeps()
        plain_sleep = deps.sleep

        def sleep(s):
            plain_sleep(s)
            deps.abort = True

        deps.sleep = sleep
        out = run_plan([PERCEIVE, {"op": "wait", "sec": 2}], go=True, deps=deps)
        row = out["results"][1]
        self.assertEqual((row["ok"], row["reason"], row["interrupted"], row["requested_sec"]),
                         (False, "aborted", "aborted", 2))
        self.assertAlmostEqual(row["waited_sec"], 0.25)
        self.assertAlmostEqual(row["waited_short_by_sec"], 1.75)
        self.assertEqual((out["ok"], out["stop_reason"], out["verification"]), (False, "aborted", "aborted"))

    def test_wait_reports_the_full_sleep_when_nothing_interrupts_it(self):
        out = run_plan([PERCEIVE, {"op": "wait", "sec": 1}], go=True, deps=(deps := FakeDeps()))
        row = out["results"][1]
        self.assertEqual((row["ok"], row["waited_sec"], row["requested_sec"]), (True, 1.0, 1))
        self.assertNotIn("interrupted", row)
        self.assertAlmostEqual(deps.now, 1.0)

    def test_wait_cut_by_the_plan_deadline(self):
        out = run_plan({"timeout_ms": 1500, "steps": [PERCEIVE, {"op": "wait", "sec": 5}]},
                       go=True, deps=(deps := FakeDeps()))
        row = out["results"][1]
        self.assertEqual((out["stop_reason"], row["ok"], row["interrupted"]), ("timeout", False, "deadline"))
        self.assertLess(row["waited_sec"], 5)
        self.assertLess(deps.now, 2.0)

    def test_op_and_time_caps(self):
        body = [{"op": "act", "norm": "1,1"}] * 4
        out = run_plan([PERCEIVE, {"op": "repeat", "times": 20, "steps": body}], go=True, deps=(deps := FakeDeps()))
        self.assertEqual((out["ok"], out["stop_reason"], out["ops_run"]), (False, "max_ops", plan_mod.MAX_OPS))
        self.assertEqual(len(deps.acts), plan_mod.MAX_OPS - 2)
        out = run_plan({"timeout_ms": 1500, "steps": [PERCEIVE, {"op": "wait", "sec": 5}, {"op": "act", "norm": "1,1"}]},
                       go=True, deps=(deps := FakeDeps()))
        self.assertEqual((out["ok"], out["stop_reason"]), (False, "timeout"))
        self.assertLess(deps.now, 2.0)
        self.assertEqual(deps.acts, [])


class TestRealDepsDryRun(unittest.TestCase):
    """Real perceive (image) + real act chain planning; a dry run never sends."""

    @classmethod
    def setUpClass(cls):
        import cv2
        import numpy as np

        cls.td = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {"CU_PERCEIVE_OUT": str(Path(cls.td.name) / "out")})
        cls.env.start()
        png = Path(cls.td.name) / "in.png"
        cv2.imwrite(str(png), np.full((200, 300, 3), 200, np.uint8))
        from cu_perceive.core import perceive

        cls.stamp = perceive(image=str(png), out_dir=str(Path(cls.td.name) / "out"))["json"]

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.td.cleanup()

    def test_act_op_plans_through_the_real_chain(self):
        out = run_plan({"stamps": {"img": self.stamp}, "steps": [
            {"op": "act", "stamp_ref": "img", "steps": [{"norm": "500,500", "text": "hi", "replace": True},
                                                        {"key": "enter", "expect_change": True}]},
            {"op": "act", "stamp": self.stamp, "norm": [100, 100], "method": "input", "check_change": True},
        ]})
        self.assertTrue(out["ok"], out)
        first = out["results"][0]
        self.assertEqual((first["step_count"], first["dry_run"]), (2, True))
        self.assertEqual([s["via"] for s in first["planned_steps"][0]], ["norm", "type"])
        self.assertEqual(out["planned_acts"], 2)
        self.assertEqual(out["frames"]["img"]["json"], self.stamp)

    def test_cli_plan(self):
        from cu_perceive.cli import main

        plan = Path(self.td.name) / "plan.json"
        plan.write_text(json.dumps([{"op": "act", "stamp": self.stamp, "norm": "500,500"}]), encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["plan", str(plan)])
        out = json.loads(buf.getvalue())
        self.assertEqual((code, out["ok"], out["dry_run"]), (0, True, True))
        self.assertIn("config_rev", out)
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", io.StringIO('[{"op": "nope"}]')):
            code = main(["plan", "-", "--validate"])
        self.assertEqual((code, json.loads(buf.getvalue())["reason"]), (1, "bad_plan"))


if __name__ == "__main__":
    unittest.main()
