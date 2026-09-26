import unittest

from harness.orchestrator.state import (
    TERMINAL,
    Failure,
    InvalidTransition,
    Observation,
    Phase,
    RunState,
)


def obs(step, paths=("a.py",)):
    return Observation(step, "read_file", "{}", True, "ok", "text", affected_paths=tuple(paths))


class PhaseTransitionTest(unittest.TestCase):
    def test_happy_path(self):
        path = (Phase.DISCOVER, Phase.PLAN, Phase.BASELINING, Phase.EXECUTE, Phase.READY_FOR_VERIFICATION,
                Phase.VERIFYING, Phase.VERIFIED)
        state = RunState(task="t", repo_root="/r")
        for phase in path:
            self.assertFalse(state.is_terminal)
            state.transition(phase, f"to {phase.value}")
        self.assertTrue(state.is_terminal)
        self.assertEqual(state.terminal_reason, "to VERIFIED")
        self.assertEqual([t.target for t in state.transitions], list(path))

    def test_repair_loop_path(self):
        state = RunState(task="t", repo_root="/r")
        for phase in (Phase.DISCOVER, Phase.PLAN, Phase.BASELINING, Phase.EXECUTE, Phase.READY_FOR_VERIFICATION,
                      Phase.VERIFYING, Phase.NEEDS_REPAIR, Phase.REPAIRING, Phase.READY_FOR_VERIFICATION,
                      Phase.VERIFYING, Phase.UNVERIFIED):
            state.transition(phase)
        self.assertEqual(state.phase, Phase.UNVERIFIED)

    def test_invalid_transitions_rejected(self):
        state = RunState(task="t", repo_root="/r")
        with self.assertRaises(InvalidTransition):
            state.transition(Phase.PLAN)          # skipping DISCOVER
        with self.assertRaises(InvalidTransition):
            state.transition(Phase.EXECUTE)
        self.assertEqual(state.phase, Phase.INTAKE)

    def test_verified_only_through_verifying(self):
        for start in Phase:
            if start == Phase.VERIFYING:
                continue
            state = RunState(task="t", repo_root="/r")
            state.phase = start
            with self.subTest(start=start), self.assertRaises(InvalidTransition):
                state.transition(Phase.VERIFIED)

    def test_repair_cannot_bypass_verification(self):
        for start, target in ((Phase.NEEDS_REPAIR, Phase.VERIFIED), (Phase.NEEDS_REPAIR, Phase.EXECUTE),
                              (Phase.REPAIRING, Phase.VERIFYING), (Phase.REPAIRING, Phase.UNVERIFIED),
                              (Phase.EXECUTE, Phase.VERIFYING), (Phase.PLAN, Phase.EXECUTE),
                              (Phase.BASELINING, Phase.READY_FOR_VERIFICATION), (Phase.VERIFYING, Phase.REPAIRING),
                              (Phase.READY_FOR_VERIFICATION, Phase.REPAIRING)):
            state = RunState(task="t", repo_root="/r")
            state.phase = start
            with self.subTest(start=start, target=target), self.assertRaises(InvalidTransition):
                state.transition(target)

    def test_terminal_phases_have_no_exit(self):
        for terminal in TERMINAL:
            state = RunState(task="t", repo_root="/r")
            state.phase = terminal
            for target in Phase:
                with self.subTest(terminal=terminal, target=target), self.assertRaises(InvalidTransition):
                    state.transition(target)

    def test_failure_recorded(self):
        state = RunState(task="t", repo_root="/r")
        state.transition(Phase.BLOCKED, "task is empty", Failure("invalid_input", "task is empty"))
        self.assertEqual(state.failure.kind, "invalid_input")
        self.assertEqual(state.summary().terminal_status, Phase.BLOCKED)


class RunStateDataTest(unittest.TestCase):
    def test_modified_files_unique_in_order(self):
        state = RunState(task="t", repo_root="/r")
        state.record_modified(["b.py", "a.py"])
        state.record_modified(["a.py", "c.py", "b.py"])
        self.assertEqual(state.modified_files, ["b.py", "a.py", "c.py"])

    def test_observations_bounded(self):
        state = RunState(task="t", repo_root="/r", max_observations=3)
        for step in range(1, 6):
            state.add_observation(obs(step))
        self.assertEqual([o.step for o in state.observations], [3, 4, 5])
        self.assertEqual(state.observations_dropped, 2)
        self.assertEqual([o.step for o in state.recent_observations(2)], [4, 5])

    def test_invalidate_marks_only_earlier_observations_of_that_path(self):
        state = RunState(task="t", repo_root="/r")
        state.add_observation(obs(1, ["a.py"]))
        state.add_observation(obs(2, ["b.py"]))
        state.add_observation(obs(3, ["a.py"]))
        self.assertEqual(state.invalidate_path("a.py", before_step=3), 1)
        self.assertEqual([o.stale for o in state.observations], [True, False, False])

    def test_counts_come_from_shared_metrics(self):
        state = RunState(task="t", repo_root="/r")
        state.metrics.record_model_call(input_tokens=1, output_tokens=1)
        state.metrics.record_tool_call("read_file", success=True)
        summary = state.summary()
        self.assertEqual((summary.model_calls, summary.tool_calls), (1, 1))
        self.assertFalse(hasattr(summary, "verified"))


if __name__ == "__main__":
    unittest.main()
