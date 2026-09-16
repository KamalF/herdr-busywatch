# The shell hook contract

busywatch works without any shell hook. The hook adds two things it cannot get
otherwise:

- **the exit status**, so a failure reads `✗ cargo (1)` instead of `✓ cargo`
- **reliability at the threshold.** The poller can only mark a command it
  sampled while running, so one that ends moments after crossing the threshold
  can slip past it — `sleep 12` against a 10-second threshold leaves a
  two-second window. The hook's file is proof on its own that a slow command
  finished, so with it installed that race disappears.

## What to write

On completion of a command that ran for at least `BUSYWATCH_MIN_SECONDS`
seconds — read from the environment, default 10 — create this directory if it
does not exist:

```
${XDG_CACHE_HOME:-$HOME/.cache}/busywatch
```

and write one file in it, named `$HERDR_PANE_ID`, containing a single line:

```
<exit status><TAB><command name>
```

`<TAB>` is one literal tab character, `0x09`. The exit status is what `wait()`
reports, so `0..255` — mask or clamp anything wider (`pwsh`'s `$LASTEXITCODE`
is a 32-bit value for a crashed native process). The poller keeps a wider value
as a failure but discards anything outside a signed 32-bit range.

Creating the directory is the hook's job: the poller only ever reads from it, so
a hook that skips the `mkdir` writes nothing on a machine where no hook has run
before.

The command name is the first word of the command line with any directory
stripped: `cargo`, not `/usr/bin/cargo build --release`.

Do nothing when `HERDR_PANE_ID` is unset — that means the shell is not inside a
herdr pane.

## Rules

- **Write, never read.** The poller consumes and deletes the file; the hook only
  produces. Do not check for it or clean it up.
- **Overwrite.** One file per pane, always truncated. The most recent command is
  the only one that matters.
- **Fail silently.** A hook that cannot write must not print anything or change
  the exit status the user sees.
- **Cheap.** This runs after every command: keep it to comparisons and, on the
  rare slow command, one small write. `busywatch.bash` also does two string
  tests per prompt to hold its place in the chain, which is the most a rule
  above costs you.

## Porting

`fish`, `zsh` and `bash` are in this directory. For another shell, find its
post-command hook and its exit-status and duration variables:

| shell | hook | status | duration |
| :---- | :--- | :----- | :------- |
| fish | `--on-event fish_postexec` | `$status` | `$CMD_DURATION` (ms) |
| zsh | `precmd` (with `preexec` to start the clock) | `$?` | `$EPOCHSECONDS` delta |
| bash | `PROMPT_COMMAND` (with a `DEBUG` trap) | `$?` | `$SECONDS` delta |
| nu | `hooks.pre_prompt` | `$env.LAST_EXIT_CODE` | own timer |
| pwsh | `prompt` function | `$LASTEXITCODE` | `Get-History` timings |

Capture the status as the **first** statement in the hook, before anything else
can overwrite it. That is the mistake every one of these is one line away from.

Four more, for any shell whose post-command hook is a *chain* of commands
rather than a single function, which is what bash's `PROMPT_COMMAND` is:

- **Run first in the chain, and keep running first.** The status you capture is
  the status of whatever ran immediately before you, so any entry ahead of
  yours has already overwritten it. Taking the position once at init is not
  enough: tools that prepend themselves at runtime (direnv does) would then
  hand you their status, and every failure would read as success.
  `busywatch.bash` re-takes first place on every prompt for this reason.
- **A `DEBUG`-style preexec fires inside the prompt hook too**, once per command
  in the chain, so it will happily time the wait at the prompt and report a
  prompt-hook command as yours. `busywatch.bash` answers that with a latch the
  prompt hook raises and a trailing entry drops.
- **Re-taking the trailing position lands a cycle late.** The shell copies the
  chain before running it, so the prompt where you move your latch release back
  to the end still runs the old order: the release fires early, and whatever
  follows it arms your timer. Skip that one cycle's release —
  `busywatch.bash` sets a flag the release consumes instead of acting on. One
  command goes unmeasured, rather than one being reported under a wrong name.
- **Such a trap has one owner, and you cannot see whose.** In bash,
  `trap -p DEBUG` is empty everywhere a sourced file can reach — the top of the
  file, and any function it calls, traced or not. To chain rather than replace,
  defer the read to the first prompt, where it works: that is what bash-preexec
  does, by appending a string to `PROMPT_COMMAND` that captures the trap and
  then installs itself. Weigh that against the latch rule above, though:
  bash-preexec's own entry lands after yours, so keeping a trailing entry last
  is out of your reach against it whatever you do.
