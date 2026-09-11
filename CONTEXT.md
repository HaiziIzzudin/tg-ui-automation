# Telegram Automator

A Windows automation context that keeps Telegram and a target window positioned, sized, and running via a persistent monitor loop.

## Language

**Primary Window**:
The Telegram window that the automator launches, resizes, and clicks inside.
_Avoid_: Main window, Telegram app

**Target Window**:
The secondary window, identified by its exact title, that the automator moves and resizes after the Click Sequence.
_Avoid_: Second window, secondary window

**Click Sequence**:
A fixed list of three clicks made at pixel offsets from the Primary Window's top-left corner; running it is what brings the Target Window into existence.
_Avoid_: Actions, clicks

**Click Target**:
One measured pixel offset from the Primary Window's top-left corner where the automator clicks. Environment-specific and tuned manually per machine.
_Avoid_: Touch target, coordinate

**Setup Sequence**:
Launch Primary Window, run the Click Sequence, then position and resize the Target Window. Run when the Primary Window process is absent.
_Avoid_: Initialization, bootstrap

**Click Retry**:
Re-running the Click Sequence to recreate a missing Target Window, without restarting the Primary Window.
_Avoid_: Re-setup, re-click

**Recovery**:
Killing the Primary Window process and relaunching it, used when Click Retry cannot restore the Target Window.
_Avoid_: Restart, reset

**Cooldown**:
The wait period after Recovery before the monitor loop checks again.
_Avoid_: Delay, backoff
