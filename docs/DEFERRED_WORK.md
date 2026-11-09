# Deferred work

Items noted during real-board use, to address later. Not yet actioned.

## Makera Z1 profile: clearance floor shadows the 0.3 mm net classes

**Logged:** 2026-11-02 (found while milling the "Blight" Tang Nano glitcher)

**Symptom.** A board built on this template routed signal tracks at 0.20 mm
spacing even though every relevant net is on the **Default** net class, whose
clearance is **0.3 mm**. KiCad DRC did not reject them, so the tight copper went
unnoticed until pcbcam's isolation check flagged ~14 copper-to-copper gaps that a
0.2 mm V-bit (0.2482 mm needed) cannot isolate.

**Cause.** The Makera profile's custom design rules
(`kibot_resources/stackups/makera_z1_2layer.kicad_dru`) contain an
**unconditional** process-floor rule:

```
(rule "Isolation clearance" (constraint clearance (min 0.2mm)))
```

In KiCad a custom-rule clearance constraint takes priority over the net-class
value, so the *enforced* clearance is pulled down to this 0.2 mm floor (bounded
only by the board minimum clearance). The 0.3 mm net classes are effectively
shadowed for clearance checking — routing is only held to the floor. Pushing the
board as the DRAFT variant (no DRC/ERC) meant nothing ever caught it.

**Why it matters.** 0.2 mm copper gaps are NOT millable with the sturdy
0.2 mm-tip V-bit at normal depth (the tip alone is 0.2 mm wide). The profile
*looks* like it guarantees 0.3 mm but doesn't, so boards drift below what the
default bit can isolate.

**Proposed fix (to decide/implement later):**
- Reconcile the floor rule with the net classes so the profile actually holds
  routing to the intended working clearance. Options:
  - Raise the unconditional `"Isolation clearance"` floor to the working value
    (e.g. 0.25-0.3 mm) so it no longer undercuts the classes; keep a separate,
    *lower* absolute floor only if a hard process minimum is still wanted, via a
    higher-priority/explicit mechanism rather than an unconditional rule.
  - OR drop the unconditional floor and let the 0.3 mm net classes govern,
    relying on the board "min clearance" (Board Setup) as the absolute floor.
- Confirm the **zone clearance** in the template's GND pours is set so a refill
  yields >= the isolation bit's kerf + margin (0.2 mm bit at 0.08 mm deep needs
  ~0.2482 mm -> use >= 0.3 mm). Changing the number alone does nothing until the
  zones are refilled (B) and the board saved.
- Decide the canonical "millable with the 0.2 mm bit" clearance for the profile
  and document it in the stackup recipe comments (`makera_z1_2l` recipe) so the
  design-rule value and the tool choice stay in sync.

**Verification idea:** add a CI/preflight note (or a `pcbcam check` reminder)
that the board must pass isolation machinability for the chosen bit, not just
KiCad DRC, before milling.
