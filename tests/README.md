# Tests

The current suite uses Python `unittest`, scripted candidates, temporary directories, and synthetic fixtures only. It runs without network, issuer data, model weights, or credentials.

The earlier 68-test suite did not cover top-level malformed candidate retry,
every Workpaper/Review/event/head crash boundary, or foreign Workpaper/Review
membership. The current source suite adds red/green regressions for those
reopened gaps, including malformed/null mixed retry replay, foreign ownership,
head-visible transaction inspection, detached commit rejection, domain-invalid
intent rejection, same-command-only pending recovery, distinct identities for
distinct malformed proposals, and receipt-history closure.
The source and isolated clean-wheel suites, the three repaired-defect
re-audits, and the renewed Standards/Spec review close the bounded scripted M2
Gate. This remains synthetic M2 evidence, not real-model, issuer, evaluation,
UI, release, or resume-admission evidence. Work hours are not an audit Gate.

Verified behavior currently covers:

- M1 and M2 scripted candidates whose frozen evidence is accepted, calculated
  with `Decimal`, persisted, and replayed by a fresh gate with no Model Adapter;
- a candidate whose claimed value disagrees with the frozen byte span and therefore yields replayable `HUMAN_REVIEW` rather than `ACCEPT`.
- registered fiscal-period, metric, basis, currency, unit, scale, and sign
  aliases, plus unresolved, ambiguous, conflicting, and cutoff cases that
  cannot reach unsafe `ACCEPT`;
- one recoverable retry, retry exhaustion after exactly two attempts,
  immediate `ABSTAIN`, and immediate `HUMAN_REVIEW` stop behavior;
- immutable task/proposal/candidate snapshots, exact source/content/profile binding, and malformed lineage identifiers;
- idempotent reruns, append-only conflict refusal, and replay of an older registered policy after the current-policy pointer advances;
- forged outcome metadata, unsupported task schema, malformed or missing
  artifacts, M1/v1 versus M2/v2 routing, and mutable process Decimal context
  during offline replay;
- closed Application commands, Case state derivation, Workpaper construction,
  human `APPROVE` / `RETURN` / `REJECT`, proposal-only export, and replay after
  restart;
- tail truncation and artifact tampering fail-closed behavior, stable public
  error families, and concurrent Case creation, analysis, replay, and shared
  immutable-document publication.

The contract suite also checks the frozen seven-symbol core surface, core v1/v2
schemas, historical M1 drafts, formal M2 Application schemas, and the public M1
demo. This is narrow synthetic M1/M2 evidence. It does not establish a real
model, authentic source acquisition, generalized filing support, an evaluation
result, or a completed Agent.
