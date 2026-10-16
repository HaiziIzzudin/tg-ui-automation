# Set the resolution only at Rotation 0 — the screen turns twice on purpose

When the screen does not match the configured Resolution or Rotation, the fix turns the screen to Rotation 0 first, sets the configured Resolution there, and only then turns to the configured Rotation. This order exists because of two hard facts on the target machines: the resolution tool silently refuses to apply a size while the screen is sideways (portrait), and the resolution tool's own orientation option is broken upstream (it is ignored without error) — which is why the automator turns the screen through the rotation library and calls the resolution tool for size only. The extra turn is the price of a size setting that actually takes.

## Considered Options

- **One call: size + orientation through the resolution tool** — rejected: the tool's orientation option is silently ignored (known unpatched upstream issue), so the automator would believe it rotated when it did not. This is the same breakage that forced the tool split in the first place.
- **Set the size in whatever rotation the screen happens to be in** — rejected: the size does not apply while the screen is sideways, so the mismatch survives every retry and the Click Sequence runs misaligned all day.
- **Turn to "any horizontal position" first (0 or 180), skipping the turn when already horizontal** — rejected: the size setting would then sometimes happen from 180, an untested position on these machines. Always routing through 0 gives one known-good size-setting position, at the cost of two extra turns when the target Rotation is 180.

## Consequences

- A fix costs two turns plus one size call when every move works on its first attempt — four turns and two size calls in the worst case — with visible flicker; every move is verified by reading the screen back and gets at most two attempts. Accepted for reliability; when everything is correct the screen is never touched.
- A failed first turn aborts the whole fix for that cycle — the size is never set on a screen that is still sideways. The size move, in turn, never blocks the final turn: the screen always finishes at the configured Rotation when the turns work, and a failed final turn is simply retried next cycle.
- The tool split is permanent, not temporary: rotation through the rotation library, size through the resolution tool without any orientation flag. A reader tempted to "simplify" this into one call reintroduces the upstream bug.
- If the resolution tool's orientation option is ever fixed upstream, the one-shot alternative becomes worth revisiting — until then it stays rejected.
