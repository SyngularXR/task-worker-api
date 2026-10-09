# Trusted borrowed-GPU ownership observer

This opt-in Linux Docker observer extends a prepared host's trusted hardware
reporter. The hardware `HostSnapshot` and its HMAC payload stay byte-compatible
with ordinary primary reporters. It is source-only until the companion backend,
worker, private configuration and measured host profile are prepared together.
No host or GPU is qualified by these fixtures or by installing the package.

The existing host-report ACK carries a list of signed `BorrowingOwners` views,
one per prepared inference binding, including disabled bindings and the period
before a first grant. The collector selects its exact authority/host/boot/epoch,
service/worker, physical GPU, policy digest and profile configuration digest.
Absent or stale first proof leaves initial borrowing ineligible. A fresh complete
signed empty owner view plus a verified physical idle view establishes eligibility
without first acquiring an inference grant.

Reporter configuration adds a `borrowing` object with `binding`, `policy`,
`journal`, `reporter_key_file` and `launchers`. Each launcher supplies its exact
worker instance, existing private Docker journal/work root, execution scope,
cgroup parent and inspected network ID. Observation never creates a missing
launch journal or launch intent. The required policy records measured driver,
collection/observation/reclaim bounds and maximum process/owner/output sizes.
There are no instance, GPU, timing or model defaults. The reporter role uses its
existing private signing key; authority clear receipts use the separate service
grant key. These files and their SQLite sidecars stay outside all model mounts.

Trusted initial preparation calls `BorrowingJournal.prepare()` in an owned 0700
parent and creates a private 0600 rollback-DELETE SQLite journal. Existing WAL or
journal-mode drift is refused without changing it. Observer run/physical-proof
identity, established state, immutable original incidents, delivery ACKs and
exact clear links survive restart. Fault writes fail closed; failed insertion is
never reported as a durable incident. New worker actions must require a proof
strictly later than that action's request, and residency must monitor continuing
successfully persisted progress. The reporter PID alone cannot detect loss of
an observer task in the same process. An old satisfied post-claim barrier is
insufficient. The companion worker applies the qualified observation deadline
and existing owned reclaim bound during loading, idle and in-flight execution.

Physical attribution uses the complete NVIDIA XML process inventory, then current
PID/start identity and unified cgroup membership against the same verified Docker
launch checks used by the supervisor. C, G, C+G and O classes require exact owned
attribution. M/M+C, MIG, unsupported cgroups/native/graphics ownership and missing
coverage remain unqualified. No task ciphertext, prompt, credential or ownership
token enters the owner view or physical evidence. A process disappearing or
appearing across views causes fresh authority and physical resampling before an
ownership contradiction is asserted.

Every verified unreleased primary owner, including expired or recovering owners,
is transient primary demand. A revoked inference owner may remain physically
present during its existing signed `reclaim_deadline`; it stays ineligible for
dispatch. That deadline derives only from the authority's first revocation time
and immutable trusted profile reclaim timeout. Reports and retries never extend
it. A still-present verified inference PID after that bound, or a freshly observed
process without an unreleased owner, is an ownership fault. Owned stops target
only the exact current borrowed inference container. Primary resources and
accounting are untouched; acknowledged physical cleanup remains necessary for
release, and release never clears an unexpected-use hold.

Hardware publication and any race-refresh POST share their existing local ordering
path. Independent fault envelopes reuse a current accepted report sequence and
fresh delivery time around the immutable original incident; they do not reserve
hardware sequence numbers or publish capacity. Primary telemetry continues during
borrowing holds, failed fault delivery and unavailable observer configuration.
Backend fault idempotence precedes freshness only for an exact prior incident.

Requalification is a separate trusted drained operation. `inspection()` requires
complete fresh empty ownership and the exact physical idle shape for this GPU.
The authority clears the full nonempty current incident set and returns a signed
receipt. `apply_clear()` independently rechecks fresh genuinely idle ownership,
physical digest/GPU, previous local binding, hold generation and exact incident
set, including on replay. It confirms named pending delivery and appends clear
links without deleting history. A newer local or remote incident defeats an older
receipt. New ordinary reports or plain service enrollment cannot clear a hold.
Fresh observer/worker action proofs remain necessary after an accepted clear.

If a committed authority clear receipt is lost and a newer incident occurs before
local application, normal `apply_clear()` still refuses the old generation.
Trusted provisioning may retrieve the exact prior clear UUID/request receipt
without reopening the newer authority hold. `reconcile_clear_history()` verifies
that signed receipt and a strict proper subset of retained incident digests while
a newer local hold remains. It appends only history and those older clear links;
binding, generation, owner/proof/run state and the newer incident remain intact.
The resulting full current set is then cleared by the ordinary idle inspection
and exact generation/set protocol. Unknown, altered, foreign-GPU or full-set
history operations are refused. Historical reconciliation is not requalification.


Same-class faults coalesce only while the same physical failure remains unresolved.
A verified physical observation or strict genuinely idle requalification inspection
records the restoration boundary. A later recurrence, even with the same bounded
condition digest and without a locally received original ACK/clear, creates a new
immutable incident UUID and generation. This does not depend on wall-clock ordering.
The older authority receipt cannot clear that new local hold; historical subset
reconciliation and a fresh full-set clear remain separate required operations.

Faults invalidate only current physical inspection/time. Strict nonempty idle
inspection records those two passive fields atomically with its exact incident-set
and generation read. It changes no binding, generation, owner/proof/run marker,
incident/ACK/clear history, pending delivery or eligibility. Empty/bootstrap, foreign
or nonidle inspection cannot record that boundary. The observer and worker still
need a fresh approved run and continuing action/progress proofs after local clear.


Trusted reenrollment on the same authority/host/physical GPU may retain and read
a fresh owner frame for the current complete binding before ordinary drained
requalification. Receiving that metadata changes only `received_owners`; it does
not rebind the journal or establish physical admission. Report sequence may reset
between bindings, but the signed authority time must be strictly later. Same-binding
sequence/time rollback and delayed old frames are refused. Readers return only
their prepared current binding, and callers must still verify its signature.
The strict inspection and full-set clear retain the old previous-binding digest
and original incident pins. A clear sets running/proof inactive; a separately
authorized observer run and fresh physical proof remain necessary. This path covers
an old pending hold whose authority clear has not yet committed. An already
committed old clear with a lost receipt followed by a binding change remains fenced.
