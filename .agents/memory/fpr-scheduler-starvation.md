---
name: FPR scheduler starvation
description: Operational constraint where long Underdog scans can continuously defer full-pool rotation work.
---

The full-pool rotation and Underdog monitoring share a serialized full-scan slot. If a live Underdog scan runs longer than the FPR interval, scheduled FPR attempts can all defer while the next Underdog scan starts immediately after completion.

**Why:** Live verification showed that the lifecycle implementation can be correct and fully tested while the scheduler still provides no execution window for FPR under long-running scans.

**How to apply:** When validating FPR in a live bot, distinguish a deferred job from a failed job and verify that at least one post-scan FPR batch actually updates persisted cursor and rotation metrics. Any remediation must preserve one FPR rotation and avoid concurrent full-pool scans.

The first live handoff after restart showed that a 10,000-row FPR batch can itself run for more than 25 minutes because evaluation and evidence persistence are performed per prop. This is separate from the handoff mechanism.

**Why:** The handoff successfully acquired the slot and prevented overlap, but the batch had not reached its final health write after an extended validation window.

**How to apply:** Treat batch-duration optimization as a separate performance task; do not reduce the batch’s evidence, scoring, delivery, or grading semantics to make scheduler validation appear faster.