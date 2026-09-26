import re
import unittest

from harness.orchestrator.state import Failure, Phase, RunState
from harness.ui import final_screen
from harness.ui.renderer import EventFeed, InteractiveRenderer, PhaseTracker, PlainRenderer, build_renderer
from harness.ui.theme import Theme

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def plain_theme():
    return Theme(color=False, unicode=False)


def unicode_theme():
    return Theme(color=True, unicode=True)


class PhaseTrackerTest(unittest.TestCase):
    def test_starts_all_pending(self):
        tracker = PhaseTracker()
        self.assertTrue(all(v == "pending" for v in tracker.status.values()))
        self.assertFalse(tracker.repair_seen)

    def test_progress_marks_prior_milestone_successful(self):
        tracker = PhaseTracker()
        tracker.apply("INTAKE", "DISCOVER")
        tracker.apply("DISCOVER", "PLAN")
        self.assertEqual(tracker.status["Discover"], "success")
        self.assertEqual(tracker.status["Plan"], "active")

    def test_verified_closes_out_verify(self):
        tracker = PhaseTracker()
        for source, target in [("INTAKE", "DISCOVER"), ("DISCOVER", "PLAN"), ("PLAN", "BASELINING"),
                               ("BASELINING", "EXECUTE"), ("EXECUTE", "READY_FOR_VERIFICATION"),
                               ("READY_FOR_VERIFICATION", "VERIFYING"), ("VERIFYING", "VERIFIED")]:
            tracker.apply(source, target)
        self.assertEqual(tracker.status["Verify"], "success")
        self.assertEqual(tracker.status["Execute"], "success")

    def test_repair_appears_in_breadcrumb_only_after_seen(self):
        tracker = PhaseTracker()
        breadcrumb_before = tracker.breadcrumb(plain_theme())
        self.assertNotIn("Repair", breadcrumb_before)
        tracker.apply("VERIFYING", "NEEDS_REPAIR")
        self.assertIn("Repair", tracker.breadcrumb(plain_theme()))

    def test_blocked_marks_current_milestone_as_failure(self):
        tracker = PhaseTracker()
        tracker.apply("INTAKE", "DISCOVER")
        tracker.apply("DISCOVER", "PLAN")
        tracker.apply("PLAN", "BLOCKED")
        self.assertEqual(tracker.status["Plan"], "failure")


class EventFeedTest(unittest.TestCase):
    def test_tool_started_then_completed_reuses_headline(self):
        feed = EventFeed(plain_theme())
        started = feed.lines_for("tool_call_started", {"tool": "read_file", "arguments": '{"path": "a.py"}'}, "EXECUTE")
        self.assertTrue(any("a.py" in line for line in started))
        completed = feed.lines_for("tool_call_completed", {"tool": "read_file", "success": True, "detail": "12 line(s)"}, "EXECUTE")
        self.assertTrue(any("a.py" in line and "12 line(s)" in line for line in completed))

    def test_failed_tool_call_uses_failure_symbol(self):
        feed = EventFeed(plain_theme())
        feed.lines_for("tool_call_started", {"tool": "run_command", "arguments": "{}"}, "EXECUTE")
        lines = feed.lines_for("tool_call_completed", {"tool": "run_command", "success": False}, "EXECUTE")
        self.assertTrue(any(line.startswith("FAIL") for line in lines))

    def test_model_call_purpose_plan_is_labelled_planning(self):
        feed = EventFeed(plain_theme())
        lines = feed.lines_for("model_call_started", {"purpose": "plan"}, "PLAN")
        self.assertTrue(any("Planning" in line for line in lines))

    def test_model_call_during_repair_is_labelled_repairing(self):
        feed = EventFeed(plain_theme())
        lines = feed.lines_for("model_call_started", {"purpose": "execute"}, "REPAIRING")
        self.assertTrue(any("Repairing" in line for line in lines))

    def test_unrelated_event_produces_nothing(self):
        feed = EventFeed(plain_theme())
        self.assertEqual(feed.lines_for("phase_changed", {"source": "PLAN", "target": "BASELINING"}, "BASELINING"), [])


class PlainRendererTest(unittest.TestCase):
    def test_output_has_no_ansi_codes(self):
        renderer = PlainRenderer(plain_theme())
        text = renderer.handle("tool_call_started", {"tool": "read_file", "arguments": '{"path": "a.py"}'}, "EXECUTE")
        self.assertIsNotNone(text)
        self.assertNotRegex(text, ANSI)

    def test_phase_changed_alone_prints_nothing(self):
        renderer = PlainRenderer(plain_theme())
        self.assertIsNone(renderer.handle("phase_changed", {"source": "PLAN", "target": "EXECUTE"}, "EXECUTE"))

    def test_finish_reuses_format_run_and_appends_artifacts_line(self):
        state = RunState(task="fix it", repo_root="/repo")
        state.phase = Phase.VERIFIED
        renderer = PlainRenderer(plain_theme())
        text = renderer.finish(state, report_path="/runs/abc123")
        self.assertIn("TASK RESULT: VERIFIED", text)
        self.assertIn("Run artifacts: /runs/abc123", text)

    def test_cancelled_mentions_no_changes_when_state_is_none(self):
        renderer = PlainRenderer(plain_theme())
        text = renderer.cancelled(None, None)
        self.assertIn("Run cancelled", text)
        self.assertIn("No changes were committed.", text)


class InteractiveRendererTest(unittest.TestCase):
    def test_emits_cursor_codes_after_first_event(self):
        renderer = InteractiveRenderer(unicode_theme())
        text = renderer.handle("discovery_completed", {"inventory_files": 10, "files_read": 2, "selected_files": 1}, "DISCOVER")
        self.assertRegex(text, ANSI)

    def test_block_height_shrinks_back_to_zero_on_finish(self):
        renderer = InteractiveRenderer(unicode_theme())
        renderer.handle("discovery_completed", {"inventory_files": 10, "files_read": 2, "selected_files": 1}, "DISCOVER")
        self.assertGreater(renderer._printed_height, 0)
        state = RunState(task="x", repo_root="/repo")
        state.phase = Phase.VERIFIED
        renderer.finish(state)
        self.assertEqual(renderer._printed_height, 0)

    def test_ascii_theme_never_emits_unicode_symbols(self):
        renderer = InteractiveRenderer(Theme(color=False, unicode=False))
        text = renderer.handle("tool_call_started", {"tool": "read_file", "arguments": '{"path": "a.py"}'}, "EXECUTE")
        for ch in "○◐✓✕—●":
            self.assertNotIn(ch, text)


def strip(text):
    return ANSI.sub("", text)


class InteractiveHistoryTest(unittest.TestCase):
    """Completed work goes to scrollback once; only the bottom region is redrawn."""

    def feed(self, renderer, events):
        return "".join(renderer.handle(name, meta, phase) or "" for name, meta, phase in events)

    def test_phase_opens_a_section_and_tool_lines_show_verb_target_detail(self):
        r = InteractiveRenderer(unicode_theme())
        out = strip(self.feed(r, [
            ("phase_changed", {"source": "BASELINING", "target": "EXECUTE"}, "EXECUTE"),
            ("tool_call_started", {"tool": "read_file", "arguments": '{"path": "src/foo.py"}'}, "EXECUTE"),
            ("tool_call_completed", {"tool": "read_file", "success": True, "detail": "120 line(s)"}, "EXECUTE"),
        ]))
        self.assertIn("╭─ Execute", out)
        line = next(l for l in out.splitlines() if "src/foo.py" in l and "✓" in l)
        self.assertIn("read", line)
        self.assertIn("120 line(s)", line)

    def test_region_has_spinner_breadcrumb_and_footer(self):
        from harness.ui.renderer import RunContext
        r = InteractiveRenderer(unicode_theme(), RunContext(repo_name="meshery", branch="harness/issue-7",
                                                            model="qwen3-coder"))
        out = strip(r.handle("model_call_started", {"purpose": "plan"}, "PLAN"))
        self.assertIn("Planning", out)
        self.assertIn("Discover", out)                       # breadcrumb
        self.assertIn("meshery (harness/issue-7)", out)      # footer
        self.assertIn("qwen3-coder", out)
        self.assertEqual(r._printed_height, 3)

    def test_tick_only_redraws_the_region(self):
        r = InteractiveRenderer(unicode_theme())
        r.handle("tool_call_started", {"tool": "run_tests", "arguments": '{"command": ["go", "test"]}'}, "EXECUTE")
        tick = r.tick()
        self.assertTrue(tick.startswith("\x1b[3A"))           # moves up over the 3-line region only
        self.assertIn("test go test", strip(tick))
        self.assertNotIn("✓", strip(tick).splitlines()[0])

    def test_token_usage_only_shown_when_reported(self):
        r = InteractiveRenderer(unicode_theme())
        out = strip(r.handle("model_call_completed", {"purpose": "execute", "input_tokens": None,
                                                     "output_tokens": None}, "EXECUTE"))
        self.assertNotIn(" in/", out)
        out = strip(r.handle("model_call_completed", {"purpose": "plan", "input_tokens": 3100,
                                                     "output_tokens": 412, "duration_ms": 2400}, "PLAN"))
        self.assertIn("3.1k in / 412 out", out)

    def test_finish_prints_panel_with_extra_sections_and_report_outside_the_box(self):
        r = InteractiveRenderer(unicode_theme())
        r.handle("phase_changed", {"source": "PLAN", "target": "EXECUTE"}, "EXECUTE")
        out = strip(r.finish(_verified_state(), "/runs/abc", extra_sections=[("Branch", ["harness/issue-7"])]))
        self.assertIn("╰─", out)                     # section closed
        self.assertIn("VERIFIED", out)
        self.assertIn("harness/issue-7", out)
        self.assertEqual(out.rstrip().splitlines()[-1].strip(), "Report /runs/abc")
        self.assertEqual(r._printed_height, 0)

    def test_harness_git_snapshots_are_hidden_during_baseline_but_shown_during_execute(self):
        r = InteractiveRenderer(unicode_theme())
        base = strip(self.feed(r, [("tool_call_started", {"tool": "git_status", "arguments": "{}"}, "BASELINING"),
                                   ("tool_call_completed", {"tool": "git_status", "success": True, "detail": "clean"},
                                    "BASELINING")]))
        self.assertNotIn("clean", base)
        run = strip(self.feed(r, [("tool_call_started", {"tool": "git_status", "arguments": "{}"}, "EXECUTE"),
                                  ("tool_call_completed", {"tool": "git_status", "success": True, "detail": "clean"},
                                   "EXECUTE")]))
        self.assertIn("clean", run)

    def test_failing_command_uses_failure_symbol_even_though_the_tool_succeeded(self):
        r = InteractiveRenderer(unicode_theme())
        out = strip(self.feed(r, [
            ("tool_call_started", {"tool": "run_tests", "arguments": '{"command": ["pytest"]}'}, "BASELINING"),
            ("tool_call_completed", {"tool": "run_tests", "success": True, "exit_code": 1, "timed_out": False,
                                     "detail": "exit 1"}, "BASELINING")]))
        line = next(l for l in out.splitlines() if "pytest" in l and "exit 1" in l)
        self.assertIn("✕", line)
        self.assertNotIn("✓", line)

    def test_baseline_summary_is_one_line(self):
        r = InteractiveRenderer(unicode_theme())
        out = strip(r.handle("baseline_completed", {"available": True, "commands": ["T1=TEST_FAILURE", "V1=PASS"]},
                             "BASELINING"))
        self.assertIn("before any edit: T1 TEST_FAILURE · V1 PASS", out)
        self.assertIn("failing now", out)

    def test_narrow_terminal_lines_never_exceed_width(self):
        theme = Theme(color=False, unicode=True, width=60)
        r = InteractiveRenderer(theme)
        out = self.feed(r, [
            ("phase_changed", {"source": "BASELINING", "target": "EXECUTE"}, "EXECUTE"),
            ("tool_call_started", {"tool": "read_file", "arguments": '{"path": "' + "a/" * 60 + 'x.py"}'},
             "EXECUTE"),
            ("tool_call_completed", {"tool": "read_file", "success": True, "detail": "9 line(s)"}, "EXECUTE"),
        ])
        for line in strip(out).splitlines():
            self.assertLessEqual(len(line), 60, line)


class BuildRendererTest(unittest.TestCase):
    def test_interactive_flag_selects_renderer_type(self):
        self.assertIsInstance(build_renderer(interactive=True, theme=plain_theme()), InteractiveRenderer)
        self.assertIsInstance(build_renderer(interactive=False, theme=plain_theme()), PlainRenderer)


def _verified_state():
    state = RunState(task="fix the bug", repo_root="/repo")
    state.phase = Phase.VERIFIED
    state.completion_claims = [(3, "fixed the thing")]
    return state


class FinalScreenTest(unittest.TestCase):
    def test_verified_panel(self):
        text = final_screen.render(_verified_state(), theme=unicode_theme())
        self.assertIn("VERIFIED", text)
        self.assertIn("fixed the thing", text)

    def test_unverified_panel_avoids_success_wording(self):
        state = RunState(task="x", repo_root="/repo")
        state.phase = Phase.UNVERIFIED
        text = final_screen.render(state, theme=unicode_theme())
        self.assertIn("UNVERIFIED", text)
        self.assertIn("does not prove", text)
        self.assertNotIn("VERIFIED\n", text.split("UNVERIFIED")[0])

    def test_blocked_panel_mentions_environment(self):
        state = RunState(task="x", repo_root="/repo")
        state.phase = Phase.BLOCKED
        state.failure = Failure("environment_error", "pytest is required but not found")
        text = final_screen.render(state, theme=unicode_theme())
        self.assertIn("BLOCKED", text)
        self.assertIn("pytest is required but not found", text)

    def test_budget_exhausted_panel(self):
        state = RunState(task="x", repo_root="/repo")
        state.phase = Phase.BUDGET_EXHAUSTED
        text = final_screen.render(state, theme=unicode_theme())
        self.assertIn("BUDGET EXHAUSTED", text)
        self.assertIn("stopped before verification", text)

    def test_ascii_theme_has_no_box_drawing_unicode(self):
        text = final_screen.render(_verified_state(), theme=plain_theme())
        for ch in "╭╮╰╯│":
            self.assertNotIn(ch, text)

    def test_cancelled_lists_modified_files(self):
        state = RunState(task="x", repo_root="/repo")
        state.modified_files = ["src/a.py"]
        text = final_screen.cancelled(state, "/runs/xyz", theme=unicode_theme())
        self.assertIn("src/a.py", text)
        self.assertIn("left in the working tree", text)
        self.assertIn("/runs/xyz", text)


if __name__ == "__main__":
    unittest.main()
