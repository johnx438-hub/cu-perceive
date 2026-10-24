"""PR19: operator abort switch (TTL, state in every result) and verification verdicts
(refused vs partial, steps_sent, chain-level abort fields). No real input: every
dispatch function is mocked."""
import os
import unittest
from unittest import mock

from cu_perceive import act as act_mod
from cu_perceive.act import annotate_verification, parse_steps, run_chain
from cu_perceive.chain import run_steps


def _screen():
    return {"stamp": "20261024-110000-001", "hwnd": None, "coord_space": "screen",
            "origin": {"left": 0, "top": 0}, "width": 1000, "height": 1000, "items": []}


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Base(unittest.TestCase):
    def setUp(self):
        act_mod.clear_abort()
        self.addCleanup(act_mod.clear_abort)
        self.clock = _Clock()
        p = mock.patch.object(act_mod, "_abort_clock", self.clock)
        p.start()
        self.addCleanup(p.stop)
        e = mock.patch.dict(os.environ, {}, clear=False)
        e.start()
        self.addCleanup(e.stop)
        os.environ.pop(act_mod.ABORT_TTL_ENV, None)

    def live(self, **extra):
        """Patches that make run_chain(_screen(), ...) dispatch into mocks only."""
        ps = [
            mock.patch("cu_perceive.act._snapshot_windows", return_value=[]),
            mock.patch("cu_perceive.windows.foreground_brief", return_value=None),
            mock.patch("cu_perceive.windows.window_at_point", return_value=77),
            mock.patch("cu_perceive.windows.foreground_matches", return_value=(True, 77)),
            mock.patch("time.sleep"),
        ]
        for k, v in extra.items():
            ps.append(mock.patch(k, **v))
        started = [p.start() for p in ps]
        for p in ps:
            self.addCleanup(p.stop)
        return started


class TestAbortMidChain(_Base):
    def test_abort_between_steps_stops_and_counts_sent(self):
        click = mock.Mock(side_effect=lambda *a, **k: act_mod.request_abort("test"))
        self.live(**{"cu_perceive.input.click_xy": {"new": click}})
        out = annotate_verification(run_chain(_screen(), parse_steps(norm="10,10;20,20;30,30"), dry_run=False))
        self.assertEqual(click.call_count, 1)
        self.assertFalse(out["ok"])
        self.assertTrue(out["aborted"])
        self.assertEqual(out["abort_step"], 1)
        self.assertEqual(out["reason"], "aborted")
        self.assertEqual(out["verification"], "aborted")
        self.assertEqual(out["evidence"]["steps_sent"], 1)
        self.assertTrue(out["abort_active"])

    def test_abort_before_start_sends_nothing(self):
        act_mod.request_abort("test")
        click = mock.Mock()
        self.live(**{"cu_perceive.input.click_xy": {"new": click}})
        out = annotate_verification(run_chain(_screen(), parse_steps(norm="10,10"), dry_run=False))
        click.assert_not_called()
        self.assertEqual(out["abort_step"], 0)
        self.assertEqual(out["verification"], "aborted")
        self.assertEqual(out["evidence"]["steps_sent"], 0)


class TestSwitchLifetime(_Base):
    def test_stays_on_until_clear_when_ttl_zero(self):
        os.environ[act_mod.ABORT_TTL_ENV] = "0"
        st = act_mod.request_abort("op")
        self.assertTrue(st["abort_active"])
        self.assertEqual(st["abort_ttl_s"], 0.0)
        self.assertIsNone(st["abort_remaining_s"])
        self.clock.t += 10_000
        self.assertTrue(act_mod.abort_active())
        click = mock.Mock()
        self.live(**{"cu_perceive.input.click_xy": {"new": click}})
        for _ in range(2):
            out = run_chain(_screen(), parse_steps(norm="10,10"), dry_run=False)
            self.assertTrue(out.get("aborted"))
        click.assert_not_called()
        self.assertEqual(act_mod.clear_abort(), {"abort_active": False})
        out = annotate_verification(run_chain(_screen(), parse_steps(norm="10,10"), dry_run=False))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["verification"], "sent")
        self.assertEqual(click.call_count, 1)

    def test_ttl_auto_clears(self):
        os.environ[act_mod.ABORT_TTL_ENV] = "5"
        act_mod.request_abort("op")
        self.clock.t += 4.0
        st = act_mod.abort_state()
        self.assertTrue(st["abort_active"])
        self.assertEqual(st["abort_remaining_s"], 1.0)
        self.clock.t += 1.0
        self.assertFalse(act_mod.abort_active())
        self.assertEqual(act_mod.abort_state(), {"abort_active": False})

    def test_repeat_request_restarts_timer(self):
        os.environ[act_mod.ABORT_TTL_ENV] = "5"
        act_mod.request_abort("op")
        self.clock.t += 4.0
        act_mod.request_abort("again")
        self.clock.t += 4.0
        st = act_mod.abort_state()
        self.assertTrue(st["abort_active"])
        self.assertEqual(st["abort_reason"], "again")

    def test_ttl_env_parsing(self):
        self.assertEqual(act_mod.abort_ttl_s(), 120.0)
        for raw, want in (("30", 30.0), ("0", 0.0), ("-1", 0.0), ("inf", 0.0), ("nope", 120.0), (" ", 120.0)):
            os.environ[act_mod.ABORT_TTL_ENV] = raw
            self.assertEqual(act_mod.abort_ttl_s(), want, raw)

    def test_dry_run_reports_abort_active(self):
        act_mod.request_abort("op")
        out = annotate_verification(run_chain(_screen(), parse_steps(norm="10,10"), dry_run=True))
        self.assertTrue(out["dry_run"])
        self.assertIsNone(out["verification"])
        self.assertTrue(out["abort_active"])
        self.assertEqual(out["abort_remaining_s"], 120.0)
        self.assertTrue(any("operator abort is active" in w for w in out.get("warnings") or []))

    def test_dry_run_without_abort_reports_inactive(self):
        out = annotate_verification(run_chain(_screen(), parse_steps(norm="10,10"), dry_run=True))
        self.assertIs(out["abort_active"], False)
        self.assertNotIn("abort_remaining_s", out)


class TestVerdicts(_Base):
    def test_refused_vs_partial(self):
        refused = annotate_verification({"ok": False, "steps": [{"via": "norm"}, {"via": "key"}]})
        self.assertEqual(refused["verification"], "refused")
        self.assertEqual(refused["evidence"]["steps_sent"], 0)
        partial = annotate_verification({"ok": False, "steps": [{"via": "norm", "clicked": True}, {"via": "key"}]})
        self.assertEqual(partial["verification"], "partial")
        self.assertEqual(partial["evidence"]["steps_sent"], 1)
        cleared = annotate_verification({"ok": False, "steps": [
            {"via": "type", "typed": False, "replace_clear": {"ok": True, "method": "select_all"}}]})
        self.assertEqual(cleared["verification"], "partial")
        waited = annotate_verification({"ok": False, "steps": [{"via": "wait", "waited": True}]})
        self.assertEqual(waited["verification"], "refused")

    def test_ok_verdicts(self):
        self.assertEqual(annotate_verification({"ok": True, "screen_changed": True, "steps": [{"clicked": True}]})
                         ["verification"], "observed")
        out = annotate_verification({"ok": True, "screen_changed": False, "changed_ratio": 0.0,
                                     "steps": [{"clicked": True}]})
        self.assertEqual(out["verification"], "sent")
        self.assertEqual(out["evidence"], {"steps_sent": 1, "screen_changed": False, "changed_ratio": 0.0})

    def test_live_failure_after_click_is_partial(self):
        click = mock.Mock()
        self.live(**{"cu_perceive.input.click_xy": {"new": click},
                     "cu_perceive.input.enter_text": {"return_value": {"ok": False, "reason": "typing_failed",
                                                                      "error": "boom"}}})
        out = annotate_verification(
            run_chain(_screen(), parse_steps(norm="10,10", text="hi", input_via="keys"), dry_run=False))
        self.assertFalse(out["ok"])
        self.assertEqual(click.call_count, 1)
        self.assertEqual(out["verification"], "partial")
        self.assertEqual(out["evidence"]["steps_sent"], 1)


class TestChainTopLevel(_Base):
    def _run(self, results):
        it = iter(results)
        steps = [{"norm": "1,1"} for _ in results]
        return run_steps(steps, lambda step, send, i: next(it), send=True)

    def test_chain_abort_keeps_both_indexes(self):
        out = self._run([
            {"ok": True, "steps": [{"clicked": True}]},
            {"ok": False, "aborted": True, "abort_step": 2, "reason": "aborted", "error": "aborted",
             "steps": [{"clicked": True}, {"tapped": True}, {"via": "key"}]},
        ])
        self.assertTrue(out["aborted"])
        self.assertEqual(out["abort_step"], 1)
        self.assertEqual(out["abort_inner_step"], 2)
        self.assertEqual(out["stop_reason"], "aborted")
        self.assertTrue(out["completed_steps"][1]["aborted"])
        out = annotate_verification(out)
        self.assertEqual(out["verification"], "aborted")
        self.assertEqual(out["evidence"]["steps_sent"], 2)

    def test_chain_failure_after_sent_step_is_partial(self):
        out = annotate_verification(self._run([
            {"ok": True, "steps": [{"clicked": True}]},
            {"ok": False, "reason": "pointer_refused", "error": "covered", "steps": [{"via": "norm"}]},
        ]))
        self.assertEqual(out["verification"], "partial")
        self.assertEqual(out["evidence"]["steps_sent"], 1)
        self.assertNotIn("aborted", out)

    def test_chain_first_step_refused(self):
        out = annotate_verification(self._run([
            {"ok": False, "reason": "stamp_check_failed", "error": "gone", "steps": [{"via": "norm"}]},
        ]))
        self.assertEqual(out["verification"], "refused")
        self.assertEqual(out["evidence"]["steps_sent"], 0)


class TestAbortTool(_Base):
    def test_tool_returns_state(self):
        from test_round9 import _collect_tools

        abort = _collect_tools()["abort"]
        self.assertEqual(abort(status=True), {"ok": True, "abort_active": False})
        st = abort(reason="stop pill")
        self.assertTrue(st["ok"] and st["abort_active"])
        self.assertEqual(st["abort_reason"], "stop pill")
        self.assertTrue(abort(status=True)["abort_active"])
        self.assertEqual(abort(clear=True), {"ok": True, "cleared": True, "abort_active": False})


if __name__ == "__main__":
    unittest.main()