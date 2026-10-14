"""Role routing must not escalate a bounded bug fix on a keyword mention (online A/B S15).

A whole-word "security", "integration", "architecture", ... in a bug description used to send
the task to ``main_engineering`` (a role the bench model ``agy_flash_38`` is not admitted for),
so admission escalated and no model call was made. The ten hard_v1 descriptions below
(task_type ``public_bugfix``) must all reach the bounded implementation role.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus_runtime.planning.engine.capability_planner import CapabilityPlanner

HARD_V1_TASKS = {
    'oabh-ledger': "Building the owner learning projection from a ledger markdown file sometimes yields fewer domains than the table contains, and no error is raised. A ledger row whose 'Current level' cell was left empty simply disappears from the projection, while a row with an invalid level such as L5 is reported as a contract violation. The projection is supposed to reflect only explicit, reviewed state and refuse ledgers that break the contract. Make the parser consistent so an incomplete ledger can never produce a silently shortened projection.",
    'oabh-remote': "The readiness check derives 'owner/name' from the checkout's `remote.origin.url` and compares it to the repository a request claims to target. Operators found that remotes which are clearly not GitHub repositories are still mapped to a valid identity: for example a remote whose hostname merely contains or ends with github.com, remote URLs followed by extra path components, a query string or a fragment, URLs carrying a user name, and paths containing '.' or '..' segments. Any of these let a request pass the source-binding check for the wrong source. The binding should only succeed for a remote that unambiguously names one GitHub repository in one of the usual clone-URL forms, and otherwise report no identity.",
    'oabh-diffhash': 'After a local-heal patch is applied in an isolated workspace, the diff that git renders can show a wider window of unchanged lines than the diff the model proposed (extra lines before and/or after the edit). The two diffs describe exactly the same change, yet their canonical hashes differ, so the selected candidate is reported as not matching what was applied. At the same time the hash must still change when the edit itself changes, when the unchanged line directly next to the edit is altered, or when context between two separate edits is altered. Make the identity independent of the extra context window without weakening those guarantees.',
    'oabh-fence': "Model probes through the Agy provider come back as a JSON envelope whose `response` field holds the model's JSON answer. Some answers arrive wrapped in a markdown code fence (a json-tagged fence containing the object, optionally followed by a trailing newline); the decoder then reports no payload even though the content is a valid object, and the probe is treated as failed. Handle that case, but keep the decoder strict about provenance: a response that has any prose before or after the fenced block, uses another fence language, or has an unterminated fence must still be rejected.",
    'oabh-activation': 'Pushing the activation manifest for a NOT_READY generation fails for every repository: variables whose desired value is empty (the activation timestamp, excluded tasks and readiness token) are sent to GitHub with an empty body, which the API refuses, so push reports failures and the repositories never reach the intended state. Even after those variables have been cleared, the verification step reports drift for them, because GitHub does not list a variable that does not exist while the manifest expects an empty value. Fix the push and verification so a NOT_READY manifest can be applied and verified cleanly, without hiding real errors or real drift.',
    'oabh-conflict': "The admission evaluator that decides whether a candidate task may mutate the working tree alongside other active writers is supposed to fail closed: anything it cannot positively prove must come back as non-CLEAR. Review found cases where it clears incorrectly. (1) A candidate or an active writer whose mutation mode is empty or missing is still admitted as if it were an isolated target. (2) The caller's `inventory_complete` flag is honoured for any truthy value such as the string 'true' or the number 1, so an unproven inventory can yield CLEAR. (3) An active writer that shares the candidate's task id and attempt id is skipped before its controller revision is compared with the expected revision, so a stale writer record from the same attempt is ignored. Make the evaluator reject all three situations with the appropriate disposition.",
    'oabh-ruleset': 'The offline audit of GitHub main-branch ruleset snapshots is supposed to emit a list of failure codes and never crash on a malformed snapshot. Two problems were found with the check that the default branch is covered. A snapshot whose `include` list is null or contains non-string entries makes the audit raise an exception instead of reporting that the default branch is not included. And a snapshot that explicitly excludes the default-branch alias from the ruleset is accepted, even though excluding the main branch itself is correctly rejected. Treat any malformed or excluded default-branch condition as a failure with the existing code.',
    'oabh-drift': 'The read-only gateway drift check alerts whenever the loaded gateway head contains commits that are not on published main. Operators report that this is too noisy for a specific case: when all of the unpublished commits only touch files under tasks/ (local task binding commits), nothing executable differs from main and the owner wants this reported as an informational, non-failing finding that points at tracking issue #1939, not as an alert with the rebind remedy. Any commit that touches a path outside tasks/ must still alert. Merge commits need to be judged by what they change relative to their first parent. If the head is also far behind main, the behind-main warning must still be reported and still determine the overall verdict.',
    'oabh-redact': "Evidence payloads are passed through a redaction step before hashing and persistence. Several credentials still leak through it. A Hugging Face, npm or AWS access-key token that is glued to a preceding identifier with an underscore (for example a variable name followed by the token) is not recognised; a short GitHub fine-grained token is not recognised; an `Authorization: token <value>` header is not redacted; and URLs whose password itself contains an '@' character have the tail of the password left in the output. In addition, the URL-credential rule can over-match in compact JSON: an '@' that appears in a later field of the same string is treated as the end of a password, which swallows unrelated fields. Fix these without making redaction slow on adversarial input and without redacting ordinary text such as hf_hub_download or content hashes.",
    'oabh-intent': "Repair tasks are being sent down the new-feature path. A task such as 'repair add_one so the verifier passes' or 'fix rebuild_index regression' is classified as a feature because the classifier looks for feature words anywhere in the text, including inside identifiers and unrelated words (address, additional, create_cache). Tasks that genuinely ask to add, create, build or implement something, or use the Chinese feature keywords, must still be treated as feature work. Standalone intent words should count; fragments of identifiers should not.",
}

COMPLEX_MENTION = (
    " This is a security-sensitive integration path in the runtime architecture, and a wrong fix "
    "could be irreversible across a cross-module boundary."
)


def _online_role(task_desc: str, task_type: str) -> tuple[str, tuple[str, ...]]:
    demands = CapabilityPlanner._build_workforce_demands(
        task_desc=task_desc,
        task_type=task_type,
        route={"online_enabled": True},
        signals=SimpleNamespace(cross_module=False),
    )
    online = [d for d in demands.demands if d.execution_channel == "online"]
    assert len(online) == 1
    return online[0].requested_role, tuple(online[0].reasons)


@pytest.mark.parametrize("task_id", sorted(HARD_V1_TASKS))
def test_hard_v1_bugfix_routes_to_bounded_implementation(task_id):
    role, _ = _online_role(HARD_V1_TASKS[task_id], "public_bugfix")
    assert role == "fast_bounded_implementation"


@pytest.mark.parametrize("task_id", sorted(HARD_V1_TASKS))
def test_bugfix_that_mentions_complex_keywords_stays_bounded(task_id):
    role, reasons = _online_role(HARD_V1_TASKS[task_id] + COMPLEX_MENTION, "public_bugfix")
    assert role == "fast_bounded_implementation"
    assert "complex_high_impact_task_main_engineering" not in reasons


@pytest.mark.parametrize(
    ("task_desc", "task_type"),
    [
        ("Redesign the runtime architecture so the planner and executor share one ledger.", "feature"),
        ("Harden the gateway's security boundary for external providers.", "feature"),
        ("Fix the token check in the gateway.", "security_fix"),
        ("Rework the cross-module contract between planner and transport.", "architecture_change"),
    ],
)
def test_genuine_complex_tasks_keep_main_engineering(task_desc, task_type):
    role, reasons = _online_role(task_desc, task_type)
    assert role == "main_engineering"
    assert "complex_high_impact_task_main_engineering" in reasons


def test_keyword_must_be_a_whole_word():
    role, _ = _online_role("Update the securityContext parsing in the loader.", "feature")
    assert role == "fast_bounded_implementation"
