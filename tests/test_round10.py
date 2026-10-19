"""PR9: guarded MCP act steps=[...] chain."""
from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cu_perceive import act as act_mod
from cu_perceive import chain
from cu_perceive.chain import MAX_STEPS, STEP_FIELDS, run_steps, validate_steps


def _ok(**extra):
    return {"ok": True, "steps": [{"via": "norm"}], "foreground_click_count": 1,
            "after": {"foreground_hwnd": 100}, "new_windows": [], **extra}


class TestValidate(unittest.TestCase):
    def test_cap(self):
        steps, err = validate_steps([{"norm": "1,1"}] * (MAX_STEPS + 1))
        self.assertIsNone(steps)
        self.assertEqual(err["reason"], "bad_args")
        self.assertEqual(err["max_steps"], 20)
        steps, err = validate_steps([{"norm": "1,1"}] * MAX_STEPS)
        self.assertIsNone(err)

    def test_bad_shapes(self):
        for bad in ([], "norm", [1], [{}], [{"action": "click"}], [{"norm": "1,1", "go": True}],
                    [{"stamp": "x", "norm": "1,1"}]):
            steps, err = validate_steps(bad)
            self.assertIsNone(steps, bad)
            self.assertEqual(err["reason"], "bad_args", bad)
            self.assertTrue(err["dry_run"])

    def test_expect_change_only_last(self):
        _, err = validate_steps([{"norm": "1,1", "expect_change": True}, {"norm": "2,2"}])
        self.assertEqual(err["reason"], "bad_args")
        self.assertEqual(err["step_index"], 0)
        steps, err = validate_steps([{"norm": "1,1"}, {"key": "enter", "expect_change": True}])
        self.assertIsNone(err)
        self.assertEqual(len(steps), 2)

    def test_underscore_keys_dropped(self):
        steps, err = validate_steps([{"norm": "1,1", "_meta": {}}])
        self.assertIsNone(err)
        self.assertNotIn("_meta", steps[0])


class TestRunSteps(unittest.TestCase):
    def _run(self, results, steps=None, send=True, titles=None):
        steps = steps or [{"norm": f"{i},1"} for i in range(len(results))]
        calls = []

        def run_one(step, send_flag, i):
            calls.append((i, send_flag))
            return results[i]

        it = iter(titles or [])
        title_now = (lambda: next(it, "T")) if titles else (lambda: "T")
        return run_steps(steps, run_one, send=send, stamp_hwnd=100, title_now=title_now), calls

    def test_dry_run_plans_every_step(self):
        out, calls = self._run([_ok(new_windows=[{"hwnd": 9}]), _ok(), _ok()], send=False)
        self.assertTrue(out["ok"])
        self.assertTrue(out["dry_run"])
        self.assertEqual(len(out["planned_steps"]), 3)
        self.assertEqual(calls, [(0, False), (1, False), (2, False)])
        self.assertFalse(out["stopped"])

    def test_new_windows_mid_chain_stops_after_that_step(self):
        res = [_ok(), _ok(new_windows=[{"hwnd": 9, "title": "登录失败"}]), _ok(), _ok()]
        out, calls = self._run(res)
        self.assertFalse(out["ok"])
        self.assertTrue(out["stopped"])
        self.assertEqual(out["stopped_at"], 1)
        self.assertEqual(out["stop_reason"], "new_windows")
        self.assertEqual(len(out["completed_steps"]), 2)
        self.assertEqual(out["remaining_steps"], [{"norm": "2,1"}, {"norm": "3,1"}])
        self.assertIn("re-perceive", out["hint"])
        self.assertEqual([c[0] for c in calls], [0, 1])

    def test_foreground_change_stops(self):
        out, _ = self._run([_ok(after={"foreground_hwnd": 555}), _ok()])
        self.assertEqual(out["stop_reason"], "foreground_changed")
        self.assertEqual(out["stopped_at"], 0)

    def test_title_change_stops(self):
        out, _ = self._run([_ok(), _ok(), _ok()], titles=["Login", "Login", "Orders"])
        self.assertEqual(out["stop_reason"], "title_changed")
        self.assertEqual(out["stopped_at"], 1)

    def test_failed_step_is_remaining_and_reason_kept(self):
        fail = {"ok": False, "reason": "focus_outside", "error": "dialog",
                "blocking_window": {"hwnd": 9, "title": "登录失败"}}
        out, calls = self._run([_ok(), fail, _ok()])
        self.assertFalse(out["ok"])
        self.assertEqual(out["stop_reason"], "focus_outside")
        self.assertEqual(out["blocking_window"]["title"], "登录失败")
        self.assertEqual(out["remaining_steps"], [{"norm": "1,1"}, {"norm": "2,1"}])
        self.assertEqual(out["completed_count"], 1)
        self.assertEqual(len(calls), 2)

    def test_typed_mismatch_and_replace_failed_stop(self):
        for reason in ("typed_mismatch", "replace_failed"):
            out, calls = self._run([{"ok": False, "reason": reason}, _ok()])
            self.assertEqual(out["stop_reason"], reason)
            self.assertEqual(len(calls), 1)

    def test_expect_change_last_is_not_a_stop(self):
        steps = [{"norm": "1,1"}, {"key": "enter", "expect_change": True}]
        out, _ = self._run([_ok(), _ok(new_windows=[{"hwnd": 9}])], steps=steps)
        self.assertTrue(out["ok"])
        self.assertFalse(out["stopped"])
        self.assertEqual(out["expected_change"], ["new_windows"])
        self.assertEqual(out["remaining_steps"], [])

    def test_unexpected_change_on_last_step_is_reported(self):
        out, _ = self._run([_ok(), _ok(new_windows=[{"hwnd": 9}])])
        self.assertTrue(out["ok"])
        self.assertEqual(out["change_after_last_step"], ["new_windows"])

    def test_submit_in_middle_stops_after_it(self):
        # step 1 is a "submit" that opens a dialog; steps 2-3 must not run
        res = [_ok(), _ok(new_windows=[{"hwnd": 7, "title": "保存成功"}]), _ok(), _ok()]
        out, calls = self._run(res)
        self.assertEqual(out["stopped_at"], 1)
        self.assertEqual(len(out["remaining_steps"]), 2)
        self.assertEqual(len(calls), 2)


def _collect():
    import test_round9

    return test_round9._collect_tools()


class TestMcpActSteps(unittest.TestCase):
    def setUp(self):
        self.frame = {"stamp": "20261020-000000-000", "hwnd": 100, "width": 100, "height": 100,
                      "origin": {"left": 0, "top": 0}}
        self.calls = []

        def fake_run_chain(frame, specs, **kw):
            self.calls.append((specs, kw))
            return _ok(dry_run=kw.get("dry_run"))

        self.patches = [
            mock.patch.object(act_mod, "load_frame", return_value=(self.frame, Path("/tmp/x.json"))),
            mock.patch.object(act_mod, "run_chain", side_effect=fake_run_chain),
            mock.patch("cu_perceive._win32.window_title", return_value="T", create=True),
        ]
        for p in self.patches:
            p.start()
        self.tools = _collect()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_flat_call_unchanged(self):
        out = self.tools["act"](stamp="s", norm="500,500")
        self.assertTrue(out["ok"])
        self.assertNotIn("chain", out)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0][1]["dry_run"])

    def test_chain_default_dry_run_plans_all(self):
        out = self.tools["act"](stamp="s", steps=[{"norm": "1,1", "text": "a", "replace": True},
                                                  {"norm": "2,2"}, {"key": "enter", "expect_change": True}])
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["dry_run"])
        self.assertEqual(len(out["planned_steps"]), 3)
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(all(kw["dry_run"] for _, kw in self.calls))

    def test_chain_live_focus_only_first(self):
        out = self.tools["act"](stamp="s", go=True, focus=True, steps=[{"norm": "1,1"}, {"norm": "2,2"}])
        self.assertTrue(out["ok"], out)
        self.assertEqual([kw["focus"] for _, kw in self.calls], [True, False])
        self.assertEqual(out["foreground_click_count"], 2)

    def test_mixing_flat_action_and_steps_is_bad_args(self):
        out = self.tools["act"](stamp="s", norm="1,1", steps=[{"norm": "2,2"}])
        self.assertEqual(out["reason"], "bad_args")
        self.assertEqual(self.calls, [])

    def test_over_cap_is_bad_args(self):
        out = self.tools["act"](stamp="s", go=True, steps=[{"norm": "1,1"}] * 21)
        self.assertEqual(out["reason"], "bad_args")
        self.assertEqual(self.calls, [])


class TestSchema(unittest.TestCase):
    def test_step_fields_are_act_params(self):
        tools = _collect()
        params = set(inspect.signature(tools["act"]).parameters)
        self.assertIn("steps", params)
        self.assertEqual(sorted(STEP_FIELDS - {"expect_change"} - params), [])
        self.assertEqual(sorted(chain.CHAIN_ONLY_FIELDS - params), [])


if __name__ == "__main__":
    unittest.main()
