# FinResearchOps / FinAuditGate Architecture Contract

> Status: `PARTIAL` overall; the locally frozen bounded scripted M2 slice is
> `COMPLETED` after renewed full-M2 Standards/Spec sign-off, source/clean-wheel
> verification, and closure of the three reopened defects. Real
> model/source/evaluation paths and UI are not implemented.

## Two-layer ownership

```text
future CLI / later UI
      ↓
FinResearchOps Application Module                         M2 COMPLETED (SCRIPTED)
  Case / Workpaper / Review / Export / replay coordination
  handle(command) / read_case(case_ref)
      ↓ public core Interface only
FinAuditGate                                               PARTIAL
  evidence and calculation admissibility
  run(task) / replay(run_ref)
      ↓
Scripted Model Adapter / local content-addressed artifacts
```

Machine `ACCEPT / RETRY / ABSTAIN / HUMAN_REVIEW` belongs to FinAuditGate.
Human `APPROVE / RETURN / REJECT` belongs to FinResearchOps. Machine `ACCEPT`
always leaves a Case in `AWAITING_REVIEW`; it never becomes human approval by
itself. Change Packets are proposal-only.

## FinAuditGate external Interface

```text
run(AuditTask) -> AuditOutcome
replay(RunRef) -> ReplayReport
```

The root package still exposes the seven frozen M1 symbols. M2 did not add a
second core entry point. Internally, M2 adds a content-bound synthetic policy
with versioned registries, explicit ambiguity and conflict decisions, a retry
budget of one, canonical proposal/candidate attempt snapshots bound to RunRef, deterministic
Decimal calculation, operand lineage, and replay/v2. Historical M1 RunRefs keep
their replay/v1 identity and eight verified artifacts; M2 uses replay/v2 and
nine.

The model can propose candidates only. It cannot verify a locator, normalize a
financial semantic, select rounding, execute code, or choose a hard decision.
Replay never calls a model, source, or network.

## FinResearchOps Application Interface

```text
handle(command) -> ApplicationOutcome
read_case(case_ref) -> CaseView
```

The closed command set is create, run analysis, submit review, export Change
Packet, and replay. Case state is derived from canonical append-only events.
The journal integrity head detects tail truncation or event replacement and
defines the committed, visible event prefix. Business-state contents still
come only from that immutable event prefix. Workpapers and
Reviews are content-addressed inside their owning Case, and per-Case locks
serialize state validation through commit across local Application instances.
The repaired candidate uses a content-addressed Case transaction intent and an
append-once commit receipt. An intent is never business authority: inspection
shows the old state while head is unchanged, and only an explicitly repeated,
canonically matching public command may finish a pending publication. Readers
independently verify every receipt against material, event, and journal prefix.
Only the current committed tail may be missing its receipt; a missing historical
receipt or an unsealed tail followed by another pending intent fails closed.
This recovery path passes its bounded independent re-audit and the renewed
full-M2 Gate without changing the public Interface.

Export eligibility is checked at runtime: latest same-Case RunRef, machine
`ACCEPT`, consistent offline replay, matching Workpaper, and append-only
`APPROVE` are all required. Packet facts and calculations are copied from the
replay-verified core ledger/formula rather than recalculated in the Application.

## Seams and remaining boundaries

- The only implemented model Adapter is scripted and synthetic.
- `FrozenDocumentPackage` freezes submitted bytes plus declared metadata; it
  does not authenticate an official source.
- Persistence uses atomic single-file append-once publication, POSIX per-Case
  locks, and a private command-authorized transaction protocol for material Case
  transitions. This declared M2 persistence model passes the full-M2 Gate;
  elapsed work time is not a Gate condition. It is not a formal proof for
  arbitrary hardware power loss or a hostile filesystem writer.
- The CLI remains a contract draft. Tests use the same Application Interface.
- A later CLI or UI must cross `handle` / `read_case` and may not duplicate
  financial or lifecycle rules.
- Real model, issuer acquisition, evaluation, public release, and UI remain
  outside M2.
