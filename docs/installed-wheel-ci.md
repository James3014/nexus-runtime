# Installed-wheel CI

`standalone-wheel.yml` builds `nexus-runtime`, installs the wheel into a fresh
Python 3.11 virtual environment, and runs the selected runtime tests from a
temporary directory outside the checkout. `PYTHONPATH` and the user site are
disabled. The job verifies that the legacy `nexus` package is unavailable, the
loaded `nexus_runtime` module is under the virtual environment's
`site-packages`, and `pip check` succeeds. It records the wheel SHA-256, source
commit/tree, interpreter, installed module path, dependency identities, export
names, and JUnit results.

The dependency file pins the build/test tools; the workflow then installs the
Learning owner at commit `1eb7229366b2856d6691c76236a50c5380dcf7d3` with build
isolation disabled after those pins are present. It does not check out or
install Nexus-new and makes no provider or model calls. The supported CI claim
is POSIX on Python 3.11; Windows and other Python versions are untested.

The frozen-donor comparison tests and their CI provisioning were retired in
Phase 4a under owner decision D2, so nothing is deselected from standalone
acceptance. `tests/integration/test_owner_workflow.py` is owner integration and
is not standalone PASS; any controlled owner run must use reviewed exact owner
pins and a mandatory import preflight, with missing owner dependencies reported
as a blocked or skipped integration result rather than PASS.

The standalone probe is deliberately fail-closed: an available legacy import,
a source-tree import, a broken installed export, a failed test, or a failed
mandatory dependency check exits nonzero. Every top-level test module is
collected with no deselections, including the retry status-set assertion.

Owner integration is controlled separately by `owner-integration.yml` (job
`Required canonical owner integration`), which runs on pull requests and on pushes
to `main` and `codex/repo-split`. It checks out each owner at an exact commit and
records expected versus actual revisions and import locations. The pins are:
nexus-core `ffe4392e964d2838fa6e4a069d4810f4436c4870` (the Core gate in
`nexus-core-issue-completion.yml` is pinned separately), nexus-learning
`1eb7229366b2856d6691c76236a50c5380dcf7d3` (same as `tests.yml`,
`standalone-wheel.yml` and `.nexus-core/config.toml`), repository-intelligence-engine
`77a937b67c3da4756f535a41b59cdf5555a25e31` (v0.1.5, same as
`repository-intelligence.yml`), and nexus-open-swe-runtime
`5da224b0c63d2b754c2fc639e26a21dc41e86d6a` (owner `main` at the time of writing; the
repository had no other pin). When any of these pins is bumped elsewhere, bump it here
and in `tests/test_owner_integration_workflow.py` (`OWNER_REFS`).

The job sets `NEXUS_OWNER_REQUIRED=1` so a missing owner dependency fails with
`OWNER_DEPENDENCY_MISSING` instead of skipping, and a contract test
(`tests/test_owner_integration_workflow.py`) asserts the pins, the flag and the
test count. Known gap: Core and Open SWE install with their transitive
dependencies unpinned (no reviewed transitive pin manifest yet), so upstream
releases can change the result. Whether the job is a required status check is an
Owner decision.
