"""Tests that the release metadata and the honesty disclosures stay true.

This project's whole claim on a reader's trust is that it does not overstate what
it has done. That makes a stale disclosure a defect in the same way a wrong return
value is: the reader acts on it. Two failure modes have already happened here and
are what these tests exist to catch.

**Test-count drift.** The README and CONTRIBUTING quote a pytest count as a
reader's expected output. When the suite grows, a quoted count silently becomes
wrong, and a contributor who sees a different number cannot tell whether they
broke something. The counts are asserted against `--collect-only`, not hardcoded
twice, so the test cannot itself go stale.

**Split disclosure.** The "never validated against a real org" claim was written
in six places: README, CHANGELOG, two docs, and two strings in `mcp_server.py`
that an AI agent reads at runtime. When Salesforce validated a bundle for the
first time, updating five of six would leave the sixth actively lying to whoever
read *that* one. So the invariant is not "the claim is present" or "the claim is
gone" — it is that every site says the *same* thing, and that whatever it says is
scoped to what was actually measured.

The specific measured fact these tests are pinned to, from lane 01:
`sf agent validate authoring-bundle -o AFT3 -n SFVB_TEST_Case_Triage --json`
returned `{"status": 0, "result": {"success": true}}` — but only after an emitter
fix, and the first submission was rejected with 24 `CompilationError`s in the
derived subagent's `reasoning:` block. That fix is now merged, so both halves have
to survive in the docs: validation happened *and* it found a real bug the project's
own `validate_locally()` could not see.

Nothing here asserts which of those two emitter states currently holds. Whether the
fix is present is asked of the emitter itself, by inspecting its output — see
`test_emitter_state_and_documented_emitter_state_agree`. A test that hardcoded
"the fix is not in this version" would have started failing the moment the fix
merged, and the tempting way to fix *that* failure is to delete the caveat.
"""

from __future__ import annotations

import ast
import itertools
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
README = REPO_ROOT / "README.md"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"
MCP_SERVER = REPO_ROOT / "src" / "sf_video_blueprint" / "mcp_server.py"

# Every file that carries the org-validation disclosure. Kept as an explicit list
# rather than a glob: the point of the test is that a *new* copy of the claim gets
# noticed, and a glob would quietly absorb one.
DISCLOSURE_SITES = (
    README,
    CHANGELOG,
    REPO_ROOT / "docs" / "mcp-release-checklist.md",
    REPO_ROOT / "docs" / "DEFECT_LEDGER.md",
    MCP_SERVER,
)

# The absolute negative claims that lane 01 falsified. Any of these appearing
# anywhere is now a false statement, because a bundle emitted by this project was
# compiled and accepted by Salesforce on 2026-07-26.
FALSIFIED_CLAIMS = (
    "has never been run against any output",
    "No real-org validation has ever occurred",
    "Nothing in this project has ever been validated against a real Salesforce",
    "No output from this server has ever been validated against a real",
    "No org has ever validated the output",
    "have never been run on pipeline output",
    "has never been run in this project",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _collected_count(*extra_env_note: str) -> int:
    """Count tests pytest actually collects in this interpreter.

    Derived rather than hardcoded so this test cannot become the next stale
    number it was written to prevent.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    match = re.search(r"^(\d+) tests collected", proc.stdout, re.MULTILINE)
    assert match, f"could not parse collection output:\n{proc.stdout[-2000:]}"
    return int(match.group(1))


class TestVersionConsistency:
    """The version in `pyproject.toml` is the single source of release truth."""

    def test_pyproject_version_is_the_prepared_release(self) -> None:
        data = tomllib.loads(_read(PYPROJECT))
        assert data["project"]["version"] == "0.1.1", (
            "v0.1.0 does not install on Python 3.12+ (playwright~=1.46.0 pins "
            "greenlet==3.0.3, whose C extension does not build against the 3.13 "
            "C API). The version bump is the deliverable that makes the latest "
            "release installable."
        )

    def test_changelog_has_a_dated_section_for_the_current_version(self) -> None:
        version = tomllib.loads(_read(PYPROJECT))["project"]["version"]
        changelog = _read(CHANGELOG)
        assert f"## [{version}] — 2026-07-26" in changelog, (
            f"CHANGELOG has no dated section for {version}. A version bump "
            "without release notes leaves a consumer no way to learn what "
            "changed or whether the install failure is fixed."
        )

    def test_changelog_link_refs_resolve_for_every_released_version(self) -> None:
        """A `[0.1.1]` heading with no matching link ref renders as literal text."""
        changelog = _read(CHANGELOG)
        headings = set(re.findall(r"^## \[([^\]]+)\]", changelog, re.MULTILINE))
        refs = set(re.findall(r"^\[([^\]]+)\]:\s*http", changelog, re.MULTILINE))
        missing = headings - refs
        assert not missing, f"CHANGELOG sections with no link ref at the bottom: {sorted(missing)}"

    def test_readme_status_heading_matches_the_shipped_version(self) -> None:
        version = tomllib.loads(_read(PYPROJECT))["project"]["version"]
        readme = _read(README)
        assert f"## Status: v{version}" in readme, (
            f"README's status heading does not name v{version}. A reader "
            "calibrates every claim below it against that version."
        )

    def test_install_instructions_do_not_point_at_the_broken_tag(self) -> None:
        """`pip install …@v0.1.0` fails outright; no runnable snippet may use it.

        Scoped to fenced code blocks. Prose *about* the broken tag is required —
        the defect table has to explain why `@v0.1.0` fails — so a line-level
        check cannot distinguish documentation from instruction. What must never
        contain it is a command a reader would copy and paste.
        """
        for path in (README, REPO_ROOT / "docs" / "mcp-install.md"):
            in_fence = False
            for line in _read(path).splitlines():
                if line.lstrip().startswith("```"):
                    in_fence = not in_fence
                    continue
                if not in_fence:
                    continue
                if "pip install" in line or "pipx install" in line:
                    assert "@v0.1.0" not in line, (
                        f"{path.name} gives a reader a runnable install command "
                        f"for the one tag that cannot be installed:\n  {line.strip()}"
                    )


class TestQuotedTestCounts:
    """A quoted pytest count is a promise to the reader; it must be measured."""

    def test_readme_and_contributing_quote_the_real_count(self) -> None:
        """The count for *this* environment must appear verbatim in both files.

        The two install profiles collect different totals, not just different
        pass/skip splits: `tests/test_mcp_server.py` calls `importorskip` at module
        scope, so without the `mcp` extra its tests are never collected. Both
        totals are therefore legitimate, and only the one matching the current
        interpreter is checkable here. CI runs both profiles, so between them
        every quoted number gets verified.

        A module-scope `importorskip` reports a skip while contributing **zero**
        collected items, so a run's `passed + skipped` exceeds `--collect-only` by
        one for every such module that actually skips. That offset is counted from
        the tree rather than assumed: hardcoding it here is how this test would
        start lying about the very number it exists to police.
        """
        collected = _collected_count()
        pattern = re.compile(r"(\d+) passed, (\d+) skipped")

        # Modules that skip at import time collect nothing but still report a skip.
        phantom_skips = sum(
            1
            for path in (REPO_ROOT / "tests").glob("*.py")
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("pytest.importorskip")
        )
        acceptable = {collected, collected + phantom_skips}

        for path in (README, CONTRIBUTING):
            quoted = pattern.findall(_read(path))
            assert quoted, f"{path.name} quotes no pytest count at all"
            totals = {int(p) + int(s) for p, s in quoted}
            assert totals & acceptable, (
                f"{path.name} quotes passed+skipped totals {sorted(totals)}, none "
                f"of which is reachable from the {collected} tests pytest actually "
                f"collects here (allowing up to {phantom_skips} import-time skip(s) "
                f"that collect nothing). A quoted count a reader cannot reproduce "
                f"is worse than no count."
            )

    def test_the_two_install_profiles_are_quoted_consistently(self) -> None:
        """[dev,mcp] and [dev]-only counts must agree between the two files."""
        pattern = re.compile(r"(\d+) passed, (\d+) skipped")
        readme = set(pattern.findall(_read(README)))
        contributing = set(pattern.findall(_read(CONTRIBUTING)))
        assert readme == contributing, (
            f"README quotes {sorted(readme)} but CONTRIBUTING quotes "
            f"{sorted(contributing)}. A contributor reading one and then the "
            "other cannot tell which is right."
        )


class TestOrgValidationDisclosure:
    """The disclosure has to move in lockstep across every site that carries it."""

    @pytest.mark.parametrize("path", DISCLOSURE_SITES, ids=lambda p: p.name)
    def test_no_site_still_makes_a_falsified_absolute_claim(self, path: Path) -> None:
        text = _read(path)
        stale = [claim for claim in FALSIFIED_CLAIMS if claim in text]
        assert not stale, (
            f"{path.name} still claims no org validation has ever happened: "
            f"{stale}. Salesforce compiled and accepted "
            "`SFVB_TEST_Case_Triage` in AFT3 on 2026-07-26 (lane 01, exit 0). "
            "A disclosure that is stale in one file is a lie in that file."
        )

    @pytest.mark.parametrize("path", DISCLOSURE_SITES, ids=lambda p: p.name)
    def test_every_site_records_what_was_actually_validated(self, path: Path) -> None:
        """Removing the old claim is only half the job — say what replaced it."""
        text = _read(path)
        assert "2026-07-26" in text or "SFVB_TEST_Case_Triage" in text, (
            f"{path.name} no longer carries the old disclaimer but does not "
            "name the validation that replaced it. An absent disclosure reads "
            "as 'fully validated', which is the overclaim this project exists "
            "to refuse."
        )

    def test_the_rejection_is_disclosed_wherever_validation_is_claimed(self) -> None:
        """The 24-error rejection has to travel with the exit-0 success.

        This is the subtle overclaim risk: "Salesforce validated our output" is
        true, and reading it as "so the output is fine" is false — the first
        attempt failed, and `validate_locally` never noticed.

        Deliberately anchored to the *section that announces the success*, not to
        the file as a whole. A whole-file check passes as soon as the number 24
        appears anywhere — the README's defect table mentions it too — so the
        caveat could be deleted from the headline while the test stayed green.
        That exact mutation was tried and did slip through a file-wide assertion.
        """
        sections = {
            README: _read(README).split("## Quick start")[0],
            CHANGELOG: _read(CHANGELOG).split("## [0.1.0]")[0],
        }
        for path, section in sections.items():
            assert re.search(r"\b24\b", section) and "reasoning" in section, (
                f"{path.name} announces org validation without recording, in that "
                "same section, that the first emitted bundle was rejected with 24 "
                "CompilationErrors in its derived `reasoning:` block. A reader who "
                "stops after the good news must not be left with the wrong belief."
            )

    def test_emitter_state_and_documented_emitter_state_agree(self) -> None:
        """Whether the fix is present is asked of the *emitter*, not of prose.

        The bundle Salesforce accepted was emitted by a FIXED `_block_scalar`. While
        that fix sat on an unmerged branch, the docs had to warn that the released
        emitter still produced the rejected shape; once it merged, that same warning
        became an understatement. Either direction is a doc defect, so this test
        derives the truth from the emitted bytes and requires the docs to match
        whichever state holds. It cannot rot into demanding a stale caveat, and it
        cannot be satisfied by deleting the caveat while the bug is still there.

        The rejected shape, verbatim from the compiler's point of view, is a
        `reasoning:` block whose next non-empty line is a bare `->`. The accepted
        shape names the key: `instructions: ->`.
        """
        from sf_video_blueprint.agent_script import build_agent_script
        from sf_video_blueprint.pipeline import run_pipeline

        result = run_pipeline(
            REPO_ROOT / "examples" / "case_triage.dom_capture.jsonl",
            org_url="https://example.my.salesforce.com",
        )
        script = build_agent_script(
            result.spec,
            developer_name="SFVB_TEST_Case_Triage",
            agent_label="SFVB TEST Case Triage",
        )
        emits_rejected_shape = re.search(r"reasoning:\s*\n\s*->\s*\n", script) is not None

        readme_head = _read(README).split("## Quick start")[0]
        warns = "not in this version" in readme_head or "NOT in this version" in readme_head

        if emits_rejected_shape:
            assert warns, (
                "The emitter still produces `reasoning:` followed by a bare `->` "
                "— the exact shape the Salesforce compiler rejected with 24 "
                "errors. The README's status section must therefore say the "
                "emitter fix is not in this version, or a reader will conclude "
                "this version emits bundles that compile. It does not."
            )
        else:
            assert not warns, (
                "The emitter now names the key (`instructions: ->`), so the "
                "compiler-rejected shape is gone, but the README still warns that "
                "the fix is 'not in this version'. That understates the project "
                "— update the status section to reflect the merged fix."
            )

    def test_local_validation_is_never_presented_as_org_validation(self) -> None:
        """`validate_locally()` returned zero findings on the rejected file."""
        text = _read(MCP_SERVER)
        assert "locallyValid" in text, "expected the MCP server to expose locallyValid"
        assert "not Salesforce's" in text, (
            "mcp_server.py must keep saying that local validation is this "
            "project's own opinion. Lane 01 measured `validate_locally()` "
            "returning zero findings on the exact file the Salesforce compiler "
            "rejected with 24 errors, so the two are demonstrably independent."
        )


class TestStatusTableHonesty:
    """The per-stage table is the most load-bearing claim in the README."""

    def _stage_row(self, stage: str) -> str:
        for line in _read(README).splitlines():
            if line.startswith(f"| {stage} ·"):
                return line
        pytest.fail(f"no status-table row for stage {stage}")

    def test_stage_5_is_still_absent(self) -> None:
        """No lane has built stage 5; the row must not be upgraded."""
        row = self._stage_row("5")
        assert "🔴 Absent" in row, (
            "Stage 5 (iterate) is still absent — `sf agent test create/run/"
            "results` appear nowhere in src/. Do not upgrade this row without "
            "measured evidence that a spec is run against a real agent."
        )

    def test_stage_5_claim_matches_the_code(self) -> None:
        """The row's justification is checkable, so check it.

        Looks at real subprocess call sites via AST rather than grepping for the
        phrase. `iterate.py` writes the literal advice "Run `sf agent test run`"
        into a generated markdown report, and it shells out to
        `agent generate agent-spec` — neither is stage 5. A grep conflates the
        prose with a call site and would fail while stage 5 is still absent.
        """
        callers = []
        for path in (REPO_ROOT / "src" / "sf_video_blueprint").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name not in {"run", "Popen", "check_output", "check_call"}:
                    continue
                argv = [
                    sub.value
                    for sub in ast.walk(node)
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                ]
                pairs = list(itertools.pairwise(argv))
                if ("agent", "test") in pairs or any("agent test" in a for a in argv):
                    callers.append(path.name)

        assert not callers, (
            f"{sorted(set(callers))} now shell out to `sf agent test` — stage 5 "
            "may no longer be absent. Re-measure and update the README row."
        )

    def test_deploy_row_records_the_scoped_validation(self) -> None:
        row = self._stage_row("6")
        assert "SFVB_TEST_Case_Triage" in row or "2026-07-26" in row, (
            "Stage 6's row must name the bundle Salesforce actually accepted, "
            "so the claim is auditable rather than a vague 'validated'."
        )

    def test_overall_grade_is_present_and_not_silently_inflated(self) -> None:
        """The headline percentage may move, but only with evidence behind it.

        Pinned to a band rather than an exact number: two of six stages remain
        🟡/🔴, so any grade at or above 70% would be unsupported by the table
        directly beneath it.
        """
        readme = _read(README)
        match = re.search(r"the honest grade is roughly \*\*(\d+)%\*\*", readme)
        assert match, "README no longer states an overall honest grade"
        grade = int(match.group(1))
        table = readme[readme.index("| Stage | Status |") :]
        unfinished = table.count("🟡") + table.count("🔴")
        assert unfinished >= 3, "status table changed shape; re-derive this bound"
        assert 55 <= grade < 70, (
            f"grade is {grade}% while {unfinished} of 6 stages are still "
            "partial or absent. A grade in the 70s is not supported by the "
            "table printed directly beneath it."
        )
