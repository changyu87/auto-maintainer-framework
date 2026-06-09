# Philosophy

*These three principles guard against silent drift — the slow, unattributable
decay of a system into a state where no one can answer "why did it do that?"
Each principle binds one orthogonal dimension: the form artifacts take, the
space they occupy, and the time they live across.*

*When principles seem to conflict, decompose the act: each principle binds a
different dimension, and conflicts are category collisions, not contradictions.
Engineering judgment is the chooser of how to act — it is not itself ranked.*

---

## 1. Machine First

Every state, metadata, interface, and artifact is designed for machine
consumption first. Handoffs use fixed-format, structured representations —
never free-form text.

Human-readable views are derivative: produced by tools that operate on the
machine-first artifact, never authored alongside it.

---

## 2. Bounded Scope

Every component operates strictly within its declared scope. Work that falls
outside that scope returns to the scope's owner; it is never assumed by the
boundary-crosser.

Cross-scope communication is contract-bound. Read nothing outside the
contract. Generate nothing outside the contract. The concrete shape of every
inter-feature contract is captured by the schema in each feature's
`docs/contract.md` (the JSON `provides` / `reads` / `invokes` / `never`
block); cross-scope reads and writes MUST conform to that schema.

---

## 3. Designed Deprecation

Every artifact is created with an explicit end-of-life criterion. Every
contract carries a version. Every component carries an owner. An unowned,
unversioned, or open-ended artifact is not a reliable artifact.

Predictability without lifecycle is borrowed time.

---
