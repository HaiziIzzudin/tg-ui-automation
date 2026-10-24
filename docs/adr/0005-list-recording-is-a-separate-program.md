# List Recording is a separate program; the automator keeps recording one stream

_Contradicts ADR-0004 (recording lives inside the automator) on one point: that record declared "one stream, no lists" and rejected a separate recording service. This decision revisits it for a second, different kind of recording._

A local bridge publishes a **Stream List**: a JSON array of everyone streaming right now, each with a stream address. People appear and disappear within seconds, so the recorder has to start and stop itself several times an hour, and several streams run at once. The automator's recording was built for the opposite shape: one address, one child, alive for hours, gated by the Schedule and the Recording Mode.

We decided to add a **List Recorder**: a second, separate program that reads the Stream List every few seconds and keeps one **List Recording** per **Listed Person**. It reuses the recorder engine and the Clips folder, and shares nothing else — no Schedule, no Recording Mode, no window work.

## Considered Options

- **Teach the automator's recorder to read the list** — rejected: the Recording Mode is one decision about one stream, and it must keep working while the automator is in Suspend. Folding per-person churn into the same loop would put a network list inside the window job and re-open the "who keeps the clock" question ADR-0002 settled.
- **Add a fourth Recording Mode value for the list** — rejected: every mode value is consulted by the monitor cycle, the dry run, and the existing tests. A second dimension (which source, how many streams) would have to be threaded through all of them, and a person leaving would then look like a Recording Stop to the window job.
- **A separate program that drives the existing engine** (chosen) — one child per Listed Person, one loop reading the list, its own log and its own settings; the Clips folder is the single thing shared. The engine gains just two optional settings (a name prefix and a retry limit) plus one status.
- **Replacing the automator's single-stream recording with the list** — rejected: the automator's stream is a fixed address known in advance and must record through Suspend; the list is about other people and must not depend on the Schedule at all. Losing the fixed-address path would be a regression.

## Consequences

- There are two entry points to run, each with its own log file and its own settings. Neither starts the other.
- Both write Clips into the same folder. List Recording Clip names lead with the person's **Clean Name**, so they never collide with the automator's timestamp-only names.
- A List Recording gets the **Retry Limit** of two attempts and then becomes **Given Up**, instead of reconnecting forever: a listed person may simply have stopped broadcasting while their entry lingers. The automator's own recording keeps retrying indefinitely, because its address is known to be right.
- List Recording Clips roll every 15 minutes instead of every 2 hours: the list changes often, and a shorter file loses less when a stream ends mid-clip. The source only makes a clean cut point about every 30 seconds, so lengths wobble.
- **Absence Grace** of two missed replies (about 10 seconds at the default 5-second poll) separates "left" from "one glitchy reply". A failed list request proves nothing at all and stops nothing.
- A future reader may try to merge the two recorders because they share an engine. The shapes are genuinely different (one fixed address versus a changing list); merging them would make the window job depend on a network list.
