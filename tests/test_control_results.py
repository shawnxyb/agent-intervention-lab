"""Regression checks for false environment acceptance from pytest log parsing."""

import pytest
from validate_evaluators import test_counts as parse_counts


@pytest.mark.parametrize(
    ("output", "passed", "failed", "errors"),
    [
        ("Installing: 400 passed\n", 0, 0, 0),
        ("================ 12 passed in 0.10s ================\n", 12, 0, 0),
        ("================ 1 failed, 5 passed in 0.12s ================\n", 5, 1, 0),
        ("================ 1 error in 0.25s ================\n", 0, 0, 1),
        ("================ 2 errors in 0.25s ================\n", 0, 0, 2),
        ("================ no tests ran in 0.01s ================\n", 0, 0, 0),
        ("================ 2 skipped in 0.01s ================\n", 0, 0, 0),
        (
            (
                "================ 10 passed in 1s ================\n"
                "================ no tests ran in 0.01s ================\n"
            ),
            0,
            0,
            0,
        ),
        (
            (
                "================ 10 passed in 1s ================\n"
                "================ 1 failed, 3 passed in 1s ================\n"
            ),
            3,
            1,
            0,
        ),
    ],
)
def test_pytest_final_summary(output, passed, failed, errors):
    counts = parse_counts(output)
    assert (counts["passed"], counts["failed"], counts["errors"]) == (passed, failed, errors)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("=== RUN   TestOne\n--- PASS: TestOne (0.00s)\nPASS\nok pkg 0.01s\n", (1, 0, 0)),
        ("=== RUN   TestOne\n--- FAIL: TestOne (0.00s)\nFAIL\n", (0, 1, 0)),
        ("FAIL pkg [build failed]\n", (0, 0, 1)),
        ("ok pkg 0.01s [no tests to run]\n", (0, 0, 0)),
    ],
)
def test_go_output_requires_actual_test_evidence(output, expected):
    counts = parse_counts(output, "go")
    assert (counts["passed"], counts["failed"], counts["errors"]) == expected


def test_go_runner_preserves_failure_status_and_prints_captured_output(tmp_path):
    import subprocess

    from validate_evaluators import runner_bytes

    runner = tmp_path / "runner.sh"
    runner.write_text(
        'set -e\nif true; then\n    TEST_OUTPUT=$(printf "--- FAIL: TestOne\\n"; exit 1)\n    TEST_EXIT_CODE=$?\nfi\necho "$TEST_OUTPUT"\nexit "$TEST_EXIT_CODE"\n'
    )
    runner.write_text(runner.read_text().replace('printf "--- FAIL:', 'printf -- "--- FAIL:'))
    result = subprocess.run(
        ["bash"], input=runner_bytes({"language": "go"}, runner), capture_output=True, check=False
    )
    assert result.returncode == 1 and b"--- FAIL: TestOne" in result.stdout
