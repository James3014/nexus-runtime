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
Learning owner at commit `d09f05b942f35236562ae26e7b718d111368b0b1` with build
isolation disabled after those pins are present. It does not check out or
install Nexus-new and makes no provider or model calls. The supported CI claim
is POSIX on Python 3.11; Windows and other Python versions are untested.

The existing full-suite workflow remains the controlled donor-comparison job.
Its donor tests are excluded from standalone acceptance because they explicitly
load the historical `/private/tmp/astra-production-integrated-20260909`
checkout. `tests/integration/test_owner_workflow.py` is owner integration and
is not standalone PASS; any controlled owner run must use reviewed exact owner
pins and a mandatory import preflight, with missing owner dependencies reported
as a blocked or skipped integration result rather than PASS.

The standalone probe is deliberately fail-closed: an available legacy import,
a source-tree import, a broken installed export, a failed test, or a failed
mandatory dependency check exits nonzero. Every top-level test module is
collected; exactly five donor comparison nodes are removed by name, while the
standalone retry status-set assertion remains.

Owner integration is controlled separately with a concrete preflight in a
separate environment. The reviewed source pins are nexus-core
`e0e04fc78b48a5c1c7fa539fe014202c7ce1788e`, repository-intelligence
`a8b9a00a6f3ea3e9ade0c6ef494d0fa88a2d73b2`, nexus-open-swe-runtime
`7f834e24365988c90c4e3e6b0d0f164a5e8c897c`, and Learning
`d09f05b942f35236562ae26e7b718d111368b0b1`. After a reviewed transitive pin
manifest is installed, run
`python -c 'import nexus_open_swe_runtime, repository_intelligence, product, nexus_learning'`,
then `pytest --strict-markers --junitxml=owner.xml tests/integration/test_owner_workflow.py`
and reject any owner result with failures, errors, or skips. A failed preflight
is blocked or skipped and cannot report integration PASS.
