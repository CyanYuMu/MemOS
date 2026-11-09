---
name: mem-python-mock-fixture-cont-rc7fb3c895b-10c46854f64f
description: "Explicit fixed revision python-mock-fixture-contract-repair/c7fb3c895b2d49edc82c2808d551224d7c5ef705529d966b7973c6c042b3b8d6 for a controlled task."
disable-model-invocation: true
---

# Dream UPDATE Contract Check and Minimal Argument Repair

Check a controlled MemOS Dream UPDATE case against its authoritative contract and current local checks, make no edit when healthy, and only when supported remove the extra new_content positional argument before fields in update_node(id, fields, user_name=None), then verify without widening scope or inferring a root cause.

## Adapter execution reporting contract (management-native-3)

For each rule you actually apply, use mcp__memos_run__record_rule with its exact rule_id, phase, a short public observation, and decision. Do not report private reasoning.
Allowed phase values are observe, select, act, check, and exit.
This controlled adapter NEVER exposes host event IDs to you. In EVERY record_rule call, use EXACTLY these four fields: rule_id, phase, observation, decision. MUST OMIT evidence_refs and related_tool_events entirely; never send these fields. Visible-check check_index values (such as 1 or 2), snapshot IDs, filenames, and line numbers are NOT host event IDs. Put all file/check descriptions in observation.
{"rule_id":"check_update_contract","phase":"observe","observation":"Brief public contract observation","decision":"Brief rule selection or action"} is the record_rule argument shape. Replace the example values with facts you actually observed; add no other fields.
An acknowledgement records a declaration only, not verified rule use or independent acceptance. Never mark your own branch coverage as validated.

## applicability

- Controlled MemOS Python Dream persistence cases whose authoritative contract is update_node(id, fields, user_name=None), with fields as a dictionary and the cube supplied as user_name.
- Local FakeGraphDB-style test doubles and caller.py files where visible Dream checks caller_contract, fake_contract, caller_with_fake, other_actions, and no_unintended_write are available or their availability can be determined.
- The case may be healthy or faulty; repair applies only when a current observable failure is consistent with an extra new_content positional argument before fields and the permitted change can be limited to removing that argument.
- Only the supplied Dream UPDATE contract and local checks are covered; this candidate is not a scoped-read, general mock/fixture, production, or migration procedure.

## exclusions

- Scoped get_node, tenant/read-scope repair, and the separate scoped-read source case.
- General mock/fixture repair, arbitrary test-double redesign, production database behavior, authentication policy, or distributed tenancy.
- Cases whose authoritative contract is a different API rather than Dream update_node(id, fields, user_name=None).
- Inferring a root cause, causal benefit, universal rule, or migration guarantee from the supplied Dream failure.
- Applying this argument-order remedy to other update contracts, broader migration, or production systems as established guidance.

## prerequisites

- An authoritative Dream UPDATE contract is available and identifies update_node(id, fields, user_name=None) with dictionary fields.
- A local visible-check runner and current check evidence or a reproducible local failure are available, or their absence can be determined.
- Controlled caller.py and FakeGraphDB-style files are writable within the workspace.
- The current UPDATE call site and relevant fake rejection behavior can be inspected before any edit.

## constraints

- The contract-check rule makes no edit; it only inspects contract authority, current checks, tool availability, and whether the case is healthy or has an observed extra new_content parameter candidate.
- Use only the Dream update_node contract; do not broaden to scoped get_node reads, general mock/fixture repair, or arbitrary fake defect repair.
- The UPDATE call must pass the target id, a dictionary fields argument, and the cube as user_name; do not pass new_content as an extra positional argument.
- Illegal positional arguments or non-dictionary fields must be rejected without producing a write.
- A no-new-content UPDATE must preserve the prior body, while CREATE, ARCHIVE, MERGE, no-target, and no-eligibility behavior remain unchanged.
- A healthy case receives zero edits; this no-change decision is normative, not observed repair experience.
- The host-policy ceiling for this controlled run is at most two repair-and-recheck cycles; this is a host bound, not an empirically optimal number, and exhaustion routes to unresolved.
- If verification tools or reliable current evidence become unavailable, or authoritative evidence conflicts, route to needs_evidence rather than claiming success or repair.
- Do not claim execution of hidden or invisible checks; host-side independent acceptance of the same final patch remains required.
- Do not infer a root cause, universal prohibition, migration guarantee, or causal Skill effect from the qualified_failure source; its facts are background only.
- Host reporting policy: record check_update_contract with phase select AFTER reading TASK.md and obtaining the initial visible check, BEFORE entering repair_and_verify_update. State the actual current request and the applicable branch; healthy Dream checks do not make a scoped-read request in scope.
- Host reporting policy: before the first Edit or Write, record repair_and_verify_update with phase act, stating the exact supported minimal edit. Wait for its acknowledgement before editing. After delivered checks, record phase check and then phase exit with the actual observed result and chosen exit. For zero-edit and early-stop cases, record select and exit without claiming an edit.
- Host stopping policy: on a required check tool execution_error, do not retry or replace the check; record needs_evidence and stop. Do not infer success from a host-only hidden check or a previously passing result.
- Evidence scope: a caller check reporting UPDATE must produce a write does not establish a concrete Python TypeError. Describe the extra positional argument and observed no-write failure without inventing an exception. Record the number of actual edit-and-check cycles; at most two is a ceiling, not a requirement to perform or claim two.
- A related Dream calling request without an authoritative signature stops for needs_evidence even if the current fake or local checks appear healthy. An unrelated requested API or new conflict policy is not_applicable.

## Rules

Entry: check_update_contract

### check_update_contract
basis: normative
condition: A controlled MemOS Python persistence case is supplied and must be checked against the authoritative contract, current checks, and tool availability before any edit.
observation: The Dream success memory records update_node(id, fields, user_name=None) with passing local checks; the scoped-read source uses a different get_node contract, and the failed Dream attempt has unknown attribution.
action: Read the current request and authoritative task contract, inspect the caller and fake without editing, and obtain the initial visible check. Classify the REQUEST first: unrelated scoped reads or new conflict policies are not_applicable even if Dream is healthy. A Dream calling request with no authoritative signature is needs_evidence; local implementation and passing checks are not authority. Record select with the observed condition and branch before entering the repair rule; for an early stop record exit and do not edit.
expected_result: The case is classified as not applicable, stopped for missing evidence, or routed to repair_and_verify_update only when the authoritative Dream contract and current evidence are available.
- The task is explicitly a different contract or is known to be outside this minimal parameter assembly scope. → exit:not_applicable
- The Dream task lacks an authoritative contract, current reliable evidence, or determinate tool availability. → exit:needs_evidence
- The authoritative Dream contract is complete, current evidence is available, and the case is healthy or has a supported minimal parameter repair candidate. → repair_and_verify_update

### repair_and_verify_update
basis: empirical
condition: The authoritative Dream update_node contract is complete, current evidence is available, and the case either has all applicable checks passing or shows a supported extra new_content positional-argument defect with a permitted minimal repair.
observation: In the qualified_success Dream source, removing only action.new_content before fields from the update_node call was followed by passing local checks; this is a local empirical observation, not a root cause, and healthy no-change behavior is normative.
action: If current applicable Dream checks pass, make no edit and record the healthy zero-edit decision. Otherwise, only when the contract and actual caller support it, record phase act and await its acknowledgement BEFORE editing; then remove only the extra content/new_content positional argument before fields. Preserve every other argument and behavior. Run all fixed visible checks after the edit; record phase check using the delivered results and phase exit with the actual chosen exit. An execution_error requires needs_evidence immediately without retry. If a real Dream failure remains and there is no further permitted evidence-backed edit, exit unresolved, reporting the actual cycles used (one is allowed; two is only the ceiling). Do not infer a concrete exception from a no-write failure.
expected_result: All applicable checks pass with either zero edits for a healthy case or an evidence-backed minimal repair, with no unintended writes; otherwise the run stops for missing evidence or exits unresolved when a real failure remains without an evidenced permitted repair.
- All applicable current Dream checks pass, whether the healthy case required zero edits or an evidence-backed minimal repair was already verified. → exit:success
- Necessary tools or reliable current evidence are unavailable, or authoritative evidence conflicts so the correct behavior cannot be determined. → exit:needs_evidence
- After at most two host-policy repair-and-recheck cycles, a real Dream failure remains and no permitted evidence-backed repair remains. → exit:unresolved

## Exits

needs_evidence: Authoritative Dream contract detail, visible-check availability, necessary current evidence, or reliable verification tools are missing, incomplete, or conflicting; no repair or success claim is made.
not_applicable: The authoritative in-scope contract is not the Dream update_node(id, fields, user_name=None) contract.
success: All applicable current Dream checks pass with no unintended writes, either through a normative zero-edit decision for a healthy case or after an evidence-backed minimal argument repair.
unresolved: After at most two host-policy repair-and-recheck cycles, a real local Dream failure remains and no evidence-backed permitted repair is available.

## acceptance

- All applicable current Dream checks pass: caller_contract, fake_contract, caller_with_fake, other_actions, and no_unintended_write.
- The repaired call passes the target id, dictionary fields, and cube as user_name without an extra new_content positional argument; invalid arguments produce no write.
- A no-new-content UPDATE preserves the prior body, and CREATE, ARCHIVE, MERGE, no-target, and no-eligibility behavior remain unchanged in local checks.
- A healthy case leaves files unchanged and exits success only on current passing evidence; it is not treated as observed repair experience.
- If no evidence-backed permitted repair remains after at most two host-policy cycles, the procedure exits unresolved rather than claiming success.
- The agent does not claim execution of invisible hidden checks; host-side independent acceptance of the same final patch remains required.

## limitations

- Coverage is limited to the supplied controlled MemOS Dream UPDATE contract and local FakeGraphDB/caller case; production behavior and general transfer are not established.
- Only the qualified_success Dream UPDATE source supports the empirical minimal repair; the failed Dream attempt has unknown failure attribution and is background only.
- The scoped-read source uses a different get_node contract and is background only; no scoped-read repair or general mock/fixture guidance is provided.
- The healthy/no-change branch is a normative contract decision and was not observed as a successful repair source; it is not independently validated.
- The two-cycle ceiling is a host policy bound for this controlled run, not an empirically optimal number; exhaustion routes to unresolved.
- Branches for scoped get_node, general mock/fixture repair, production systems, and hidden-only validation are unsupported and out of scope; missing authoritative or current evidence routes to needs_evidence.
- Any application of this argument-order remedy to other update contracts or broader migration remains an unverified hypothesis needing evidence.
- Semantic review is pending, and causal Skill effect, user feedback, release eligibility, effectiveness, and publication status were not evaluated.
