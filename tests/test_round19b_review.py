"""PR19 review round (DSH): abort hint on chains, TTL pinned per act, every MCP act return
annotated, no focus/IME side effects while stopped, partial typing counted, abort tool edges.
No real input: every dispatch function is mocked."""
import os
import unittest
from unittest import mock

from cu_perceive import act as act_mod
from cu_perceive import input as input_mod
from cu_perceive.act import annotate_verification, parse_steps, run_chain
from cu_perceive.chain import RE_PERCEIVE_HINT, run_steps

from test_round19_abort import _Base, _screen


class TestChainAbortHint(_Base):
    def test_abort_hint_not_resume_hint(self):
        results = iter([
            {"ok": True, "steps": [{"clicked": True}]},
            {"ok": False, "aborted": True, "abort_step": 0, "reason": "aborted",
             "error": "aborted by operator before step 0", "steps": [{"via": "norm"}],
             "evidence": {"abort_step": 0, "abort_reason": "stop pill"}},
        ])
        out = run_steps([{"norm": "1,1"}, {"norm": "2,2"}, {"norm": "3,3"}],
                        lambda s, send, i: next(results), send=True)
        self.assertEqual(out["hint"], act_mod.ABORT_HINT)
        self.assertNotEqual(out["hint"], RE_PERCEIVE_HINT)
        self.assertEqual(out["reason"], "aborted")
        self.assertNotIn("failed", out["error"])
        self.assertEqual(out["remaining_steps_not_run_reason"], "operator_abort")
        out = annotate_verification(out)
        self.assertEqual(out["verification"], "aborted")
        self.assertEqual(out["evidence"]["abort_reason"], "stop pill")
        self.assertEqual(out["evidence"]["steps_sent"], 1)

    def test_guard_stop_keeps_resume_hint(self):
        results = iter([{"ok": False, "reason": "pointer_refused", "error": "x", "steps": []}])
        out = run_steps([{"norm": "1,1"}], lambda s, send, i: next(results), send=True)
        self.assertEqual(out["hint"], RE_PERCEIVE_HINT)


class TestTtlPinnedPerAct(_Base):
    def test_stop_raised_mid_act_survives_ttl_during_that_act(self):
        os.environ[act_mod.ABORT_TTL_ENV] = "5"

        def slow_click(*a, **k):
            act_mod.request_abort("op")
            self.clock.t += 10.0  # the step outlives the TTL

        click = mock.Mock(side_effect=slow_click)
        self.live(**{"cu_perceive.input.click_xy": {"new": click}})
        out = annotate_verification(run_chain(_screen(), parse_steps(norm="10,10;20,20;30,30"), dry_run=False))
        self.assertEqual(click.call_count, 1)
        self.assertTrue(out["aborted"])
        self.assertEqual(out["abort_step"], 1)
        # After the act, the real clock applies again: the TTL has run out.
        self.assertFalse(act_mod.abort_active())

    def test_stop_on_at_act_start_stays_on_for_whole_act(self):
        os.environ[act_mod.ABORT_TTL_ENV] = "5"
        act_mod.request_abort("op")
        self.clock.t += 4.0
        with act_mod.abort_epoch():
            self.clock.t += 100.0
            self.assertTrue(act_mod.abort_active())
        self.assertFalse(act_mod.abort_active())

    def test_expired_before_act_start_lets_act_run(self):
        os.environ[act_mod.ABORT_TTL_ENV] = "5"
        act_mod.request_abort("op")
        self.clock.t += 6.0
        click = mock.Mock()
        self.live(**{"cu_perceive.input.click_xy": {"new": click}})
        out = annotate_verification(run_chain(_screen(), parse_steps(norm="10,10"), dry_run=False))
        self.assertTrue(out["ok"], out)
        self.assertEqual(click.call_count, 1)


class TestNoSideEffectsWhileStopped(_Base):
    def test_no_focus_or_ime_when_aborted(self):
        act_mod.request_abort("op")
        bring = mock.Mock(return_value=None)
        ime = mock.Mock(return_value=None)
        click = mock.Mock()
        self.live(**{"cu_perceive.input.click_xy": {"new": click},
                     "cu_perceive.act._bring_forward": {"new": bring},
                     "cu_perceive.act._suspend_ime_for_act": {"new": ime},
                     "cu_perceive.act._has_window": {"return_value": True}})
        out = run_chain(_screen(), parse_steps(norm="10,10"), dry_run=False, focus=True)
        self.assertTrue(out.get("aborted"), out)
        self.assertEqual(out["abort_step"], 0)
        bring.assert_not_called()
        ime.assert_not_called()
        click.assert_not_called()


class TestPartialTyping(_Base):
    def test_type_text_failure_reports_sent_chars(self):
        calls = {"n": 0}

        def send(_inp):
            calls["n"] += 1
            if calls["n"] == 5:  # 3rd character's key-down
                raise OSError("SendInput failed")

        with mock.patch.object(input_mod, "_send", side_effect=send), mock.patch("time.sleep"):
            res = input_mod.enter_text("abcdef", via="keys")
        self.assertFalse(res["ok"])
        self.assertEqual(res["sent_chars"], 2)

    def test_partial_typing_is_partial_not_refused(self):
        self.live(**{"cu_perceive.input.click_xy": {"new": mock.Mock()},
                     "cu_perceive.input.enter_text": {"return_value": {
            "ok": False, "reason": "input_failed", "error": "boom", "sent_chars": 3}}})
        out = annotate_verification(run_chain(
            _screen(), parse_steps(norm="10,10", text="hello", input_via="keys"), dry_run=False))
        self.assertFalse(out["ok"])
        self.assertEqual(out["verification"], "partial", out)
        self.assertEqual(out["evidence"]["steps_sent"], 2)  # the click and the half-typed text
        self.assertTrue(out["steps"][-1]["typed_partial"])
        self.assertEqual(out["steps"][-1]["sent_chars"], 3)

    def test_typing_failure_before_any_char_not_counted(self):
        self.live(**{"cu_perceive.input.click_xy": {"new": mock.Mock()},
                     "cu_perceive.input.enter_text": {"return_value": {
            "ok": False, "reason": "input_failed", "error": "boom", "sent_chars": 0}}})
        out = annotate_verification(run_chain(
            _screen(), parse_steps(norm="10,10", text="hello", input_via="keys"), dry_run=False))
        self.assertEqual(out["evidence"]["steps_sent"], 1, out)  # only the click
        self.assertNotIn("typed_partial", out["steps"][-1])


class TestMcpEveryReturnAnnotated(_Base):
    def setUp(self):
        super().setUp()
        from test_round9 import _collect_tools

        self.tools = _collect_tools()

    def _check(self, out, verdict):
        self.assertIn("abort_active", out)
        self.assertIn("verification", out)
        self.assertEqual(out["verification"], verdict, out)
        if verdict is not None:
            self.assertEqual(out["evidence"]["steps_sent"], 0)

    def test_load_error_annotated(self):
        out = self.tools["act"](stamp="19990101-000000-000", norm="1,1", go=True)
        self.assertFalse(out["ok"])
        self._check(out, "refused")

    def test_bad_args_annotated(self):
        out = self.tools["act"](norm="not-a-point", go=True)
        self.assertEqual(out.get("reason"), "bad_args", out)
        self._check(out, "refused")

    def test_bad_args_dry_run_annotated(self):
        out = self.tools["act"](norm="not-a-point")
        self._check(out, None)

    def test_bad_chain_live_is_refused_not_dry_run(self):
        out = self.tools["act"](steps=[{"bogus": 1}], go=True)
        self.assertEqual(out.get("reason"), "bad_args", out)
        self.assertFalse(out["dry_run"])
        self._check(out, "refused")

    def test_abort_active_visible_on_refusal(self):
        act_mod.request_abort("op")
        out = self.tools["act"](stamp="19990101-000000-000", norm="1,1", go=True)
        self.assertTrue(out["abort_active"])


class TestAbortToolEdges(_Base):
    def setUp(self):
        super().setUp()
        from test_round9 import _collect_tools

        self.abort = _collect_tools()["abort"]

    def test_clear_echoes_reason(self):
        self.abort(reason="stop pill")
        out = self.abort(clear=True)
        self.assertEqual(out, {"ok": True, "cleared": True, "abort_active": False, "cleared_reason": "stop pill"})

    def test_clear_and_status_is_bad_args(self):
        self.abort(reason="op")
        out = self.abort(clear=True, status=True)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "bad_args")
        self.assertTrue(out["abort_active"])  # nothing was cleared

    def test_reason_truncated(self):
        st = self.abort(reason="x" * 5000)
        self.assertEqual(len(st["abort_reason"]), act_mod.ABORT_REASON_MAX)


if __name__ == "__main__":
    unittest.main()