# busywatch

A herdr plugin that answers one question at a glance: **is anything still
running, and does it need me?**

herdr tracks coding agents well. Everything else in a pane reports
`agent_status: "unknown"` and looks like an idle shell: a build, a test run, a
deploy, a server. busywatch fills that gap. It registers none of these as an
agent, so the Agents panel stays a list of real agents.

```
spaces                              tabs                    panes of tab 1
                                ┌──────┬──────┬───┐   ┌─ ▸ cargo 4m12s ─┬─ × pytest (1) ─┐
  ● api   ▸ cargo ‖ × pytest    │ × 1  │ ‖ 2  │ 3 │   │ still building  │ failed while   │
  ● web   ‖                     └──────┴──────┴───┘   │                 │ you were away  │
  ● infra × 2                                         └─────────────────┴────────────────┘
```

The picture uses look-alike glyphs, because the real marks in the table below
render at odd widths in a browser's monospace font and break the boxes.

One session, drilled into. `api` has a build running and a test run that
failed while you were away, both in tab 1. Tab 1 therefore carries the stronger
of the two marks. Tab 2 has something that waits on you.

| Mark | Meaning |
| :--- | :------ |
| `▶` | a command is running |
| `⏸` | it waits on **you**: a prompt for input, or an agent asking permission |
| `✓` | it finished while you were looking somewhere else |
| `✗` | it finished and failed. This mark needs a shell hook. Without one, a failure shows as `✓` |
| `⚙` | a Claude agent that looks idle still has background work in flight. `⚙ 4` for four tasks, `✓` once they finish unseen |

Marks narrow as you drill in. The Spaces panel says which workspace. The tab
bar says which tab, without opening anything. The pane label says which pane
and for how long. `✓` and `✗` are sticky: they stay until you focus that pane.
That is what makes them useful for something that ended an hour ago.

A mark on the Space row names its command when one pane carries it: `▶ cargo`,
`✗ pytest`. When two panes of the space carry the same mark, the row has no
room for both names and gives a count instead: `✗ 2`. Set `names = off` to keep
the glyphs and drop every name, and see *How much the row says* for that and
for how many commands a space lists by name.

## Install

```bash
herdr plugin install KamalF/herdr-busywatch
```

This covers every mark but `✗`, which needs the shell hook below. `⏸` for
shell panes also needs `kernel.yama.ptrace_scope=0`. Some distributions ship
`1`. Make sure of yours with `sysctl kernel.yama.ptrace_scope` (see Limits).
busywatch requires herdr 0.9.0 or later and Python 3.9 or later.

Add the tokens to your sidebar layout in `~/.config/herdr/config.toml`:

```toml
[ui.sidebar.spaces]
rows = [
  ["state_icon", "workspace",
   { token = "$run", fg = "#8ec07c" },
   { token = "$wait", fg = "#fabd2f", bold = true },
   { token = "$done", fg = "#83a598", bold = true }],
  ["branch", "git_status"],
]

[ui.sidebar.agents]
rows = [
  ["state_icon", "machine", "workspace", "tab",
   { token = "$bg", fg = "#d3869b", bold = true }],
  ["agent"],
]
```

herdr does not watch `config.toml`. Apply an edit with:

```bash
herdr server reload-config
```

When the layout is accepted, the command answers `"status": "applied"` with an
empty `diagnostics` list. This doubles as the syntax check.

Then start the poller once. The plugin's `[[startup]]` hook runs only when the
herdr server starts, so a fresh install shows nothing until then. Restart the
server, or run the **Restart busywatch** action from the workspace menu.
`bin/busywatch-start` does the same by hand. Each of these also links the shell
hooks into place for the section below.

The tab glyph needs no configuration. The pane label is a reported pane title,
so it obeys herdr's own `show_agent_labels_on_pane_borders`, which ships
`false`. Turn it on to get `▶ cargo 4m12s` on the pane border. herdr draws
these labels only on the border of a split pane, and only while the pane has no
manual name of its own.

### A row per running command (optional)

`$run` names the command when one runs in the space, and counts them when more
do: `▶ 2`. To read every name, add the numbered tokens. Each one holds one
command, and `run_slots` sets how many get filled: three by default, up to
nine. herdr draws a row per entry in `rows`, and a row whose tokens are all
empty takes no height, so a slot costs nothing while it is unused.

```toml
[ui.sidebar.spaces]
rows = [
  ["state_icon", "workspace",
   { token = "$wait", fg = "#fabd2f", bold = true },
   { token = "$done", fg = "#83a598", bold = true }],
  [{ token = "$run1", fg = "#8ec07c" }],
  [{ token = "$run2", fg = "#8ec07c" }],
  [{ token = "$run3", fg = "#8ec07c" }],
  ["branch", "git_status"],
]
```

```
qlack
  ▶ cargo
  ▶ npm
```

The slots are ordered by pane, so a name does not change rows from one tick to
the next. When more commands run than there are slots, the last filled one
counts the rest: `▶ +4`. The slots stand beside `$run`, which keeps its own
shape, so a layout can carry either or both.

Put as many `$runN` rows in the layout as `run_slots` fills. Fewer, and the
count of what did not fit lands in a slot the layout never draws, so a space
running four commands shows two names and no sign of the other two. To show
less, lower `run_slots` rather than dropping a row: the `▶ +N` then moves up
into a slot you can see.

### Exit codes (optional, one line per shell)

The hooks are linked under `~/.local/state/busywatch`, so there is no plugin
root to look up. If you set `XDG_STATE_HOME`, the directory is
`$XDG_STATE_HOME/busywatch` instead. Substitute it in every path below.

```bash
# fish
ln -s ~/.local/state/busywatch/busywatch.fish ~/.config/fish/conf.d/
# zsh, in ~/.zshrc
source ~/.local/state/busywatch/busywatch.zsh
# bash, in ~/.bashrc, last
source ~/.local/state/busywatch/busywatch.bash
```

The links are refreshed every time the plugin's start script runs, so they
follow an upgrade that moves the plugin. zsh and fish have real hook lists and
need no ordering. bash is the exception, below.

The hook is the only shell-specific piece, and it is the only route to an exit
status. The poller sees a command start and stop, not whether it worked.
herdr's API exposes no exit status for a pane, so a plugin cannot read the
shell-agnostic alternative, `OSC 133;D;<code>`, either. The hook also closes a
race. [shell/CONTRACT.md](shell/CONTRACT.md) tells both stories and is all you
need to port the hook.

#### On bash: what else owns your prompt

bash has no preexec, so the hook uses the DEBUG trap. The hook takes both ends
of `PROMPT_COMMAND`. It runs first, to capture `$?` before anything else can
overwrite it. It runs last, to release the latch that keeps the timer off the
prompt hook's own commands.

It re-takes both places on **every** prompt, not only at source time. Otherwise
a tool that prepends itself later, as direnv does, hands the hook its own exit
status, and every failure reads as success. Three cases follow.

A neighbour that prepends is fine, in either order. It ends up second,
permanently, and busywatch still sees the `$?` of your command first:

```bash
# ~/.bashrc
eval "$(direnv hook bash)"
source ~/.local/state/busywatch/busywatch.bash
```

Against a hand-rolled DEBUG trap, source busywatch last. A DEBUG trap has one
owner. A sourced file cannot see the trap that it replaces, because
`trap -p DEBUG` reads empty there. Nothing warns you:

```bash
# wrong: the later trap silently replaces the hook's, and no mark is ever ✗
source ~/.local/state/busywatch/busywatch.bash
trap 'my_own_timer' DEBUG

# right
trap 'my_own_timer' DEBUG
source ~/.local/state/busywatch/busywatch.bash
```

A neighbour that appends is bash-preexec, which `atuin init bash` embeds. It
takes both ends of `PROMPT_COMMAND` for itself, plus the DEBUG trap or, from
0.6 on bash 5.3, `PS0`. On bash 5.1 and later it turns `PROMPT_COMMAND` into
an array, so nothing sourced beside it can hold the trailing slot. When
bash-preexec is already loaded, the hook registers with `preexec_functions`
and `precmd_functions` instead, and installs no trap and no `PROMPT_COMMAND`
entry of its own. bash-preexec then does the latch's job and hands the hook
each command's exit status itself.

bash-preexec must be loaded first, so busywatch stays last:

```bash
eval "$(atuin init bash)"      # or: source ~/.bash-preexec.sh
source ~/.local/state/busywatch/busywatch.bash
```

Sourced before bash-preexec, the hook cannot see it and installs the DEBUG
trap path, and the fight is back: with bash-preexec 0.6 your commands are
reported under bash-preexec's own function names, and with 0.5 nothing is
reported at all. The order above is a requirement, not a preference.

bash-preexec has blind spots of its own, and atuin shares them. It reads the
command line back from history, so a command that `HISTIGNORE` keeps out of
history is reported under the previous command's name. Versions before 0.6,
and any version on bash before 5.3, hook the DEBUG trap and skip it when
stdout is not a terminal, so there a compound command with a redirection,
`{ make; } > log`, is not reported at all, and neither is a bare subshell
line, `(make)`. A plain `make > log` is.

## How it works

One poller, every 2 seconds, over herdr's unix socket:

```
pane.list
  ├─ agent / agent_session / status != unknown  →  herdr's; roll up blocked,
  │                                                mark Claude background work
  └─ otherwise
       └─ pane.process_info             (if its revision changed, or it is busy)
            └─ foreground_process_group_id != shell_pid  →  busy
                 └─ aggregate per workspace and tab
                      └─ workspace.report_metadata / pane.report_metadata / tab.rename
```

A command must run for **10 seconds** before it counts, so `ls` and `git status`
never flicker a mark up.

**Cost: about 20ms a tick, about 1% of one core** on a 47-pane session. Three
things keep it there.

- Requests go over the socket rather than the CLI: about 0.7ms against about
  10ms, because the CLI spawns a process. Through a mise shim the CLI takes
  about 190ms, enough to stretch a tick past every TTL and make the whole
  sidebar flicker.
- `pane.process_info` is one request per pane and dominates everything else.
  The poller asks it only for panes whose output revision changed, or that are
  already known busy. A periodic full sweep bounds how long a missed start can
  hide.
- The Claude task files are found through a remembered project directory. The
  obvious glob puts a `*` where that directory goes and re-lists the whole of
  `/tmp/claude-$UID` once per idle Claude pane per tick. That is 3.4ms against
  0.02ms here, enough on its own to blow the budget above.

### Waiting on you

`⏸` comes from `/proc/<pid>/syscall`, which names the syscall a process is
stopped in, along with its arguments. A process blocked in `read()` on a file
descriptor that resolves to a terminal waits for you:

| process | syscall | verdict |
| :------ | :------ | :------ |
| `cat` blocked on the tty | `0 0x0`, that is `read(0)` | `⏸` |
| `sleep 300` | `230`, `clock_nanosleep` | `▶` |
| a server parked on a futex | `202`, `futex` | `▶` |

The poller resolves the fd and requires a terminal. This keeps reads from files
and pipes out, and catches tools that prompt on `/dev/tty` rather than stdin.
Matching `/proc/<pid>/wchan` against tty-read symbol names looks simpler but is
not reliable: a blocked `cat` can report `wait_woken`, a generic wait helper.

Syscall `0` is `read()` on x86_64. On another architecture, adjust
`waiting_on_input`.

### Claude background work

Claude Code writes one `.output` file per background task under
`/tmp/claude-$UID/<project>/<session>/tasks/`. A pane's `agent_session.value`
is that session id, so no hook is needed to find them:

- `b…`: a backgrounded `Bash` command. It is done when its **last line** carries
  `[exited with code N]`, `[killed]`, or `[process exited while detached…]`.
  The last line, not the whole file, so a task whose own output mentions one of
  these does not read as finished.
- `a…`: a background subagent, a JSONL transcript. It is done when the last
  record is an `assistant` turn that carries a `stop_reason`.

A file untouched for 15 minutes counts as finished, whatever it says. Verdicts
are cached on `(mtime, size)`, so a settled file is read once. The count shows
only while the agent is `idle` or `done`. A foreground command writes an
`.output` file too, so a count during a turn only restates the spinner. A `✓`
already earned stays until you focus the pane, also through a later turn.

There is no hook-based alternative for the case that matters. Claude Code's
`SubagentStop` hook fires when an in-session subagent finishes, and `Stop` and
`SubagentStop` both carry `background_tasks`. But **nothing fires when a
backgrounded `Bash` command exits**, which is the other half of `⚙`. Nothing
fires at the moment background work ends while the agent itself stays idle.
That transition is exactly what this mark exists to show.

## State and cleanup

Marks are written with `ttl_ms`. A poller that is killed takes them with it
within a few seconds, and there is nothing to clean up.

The tab name is the exception, and it costs something. `tab.rename` is the only
way to mark a tab, and it carries no TTL. It sets the tab's *custom name*.
Nothing can set that back to unset, and `TabInfo` does not distinguish it from
the positional label it reports. So the glyph goes on as a **strippable
prefix**, and two things follow:

- A marked tab keeps a custom name for good, equal to the label it carried when
  it was marked. For a tab you named yourself this is invisible. An unnamed tab
  keeps the position number it had, which shows once you move or close tabs.
  Rename it yourself to clear it.
- The first tick of every run strips a leading glyph from every tab. This is how
  the marks of a crashed poller (at most one per tab) get cleaned up. It also
  means that a tab *you* named with a leading `▶`, `⏸`, `✓` or `✗` and a space
  loses that prefix once. The poller cannot tell your prefix from its own.

## Running it

The plugin's `[[startup]]` hook starts the poller and exits. herdr startup hooks
are one-shot rather than supervised, so `bin/busywatch-start` spawns the poller
detached and keeps a pidfile at
`${XDG_STATE_HOME:-~/.local/state}/busywatch/busywatch.pid`. A second run is a
no-op, which makes it safe on every server start and live handoff. The pid in
that file must still belong to a poller to count. A stale pidfile left by a
reboot therefore neither blocks a start nor signals an unrelated process.

The poller's own output lands next to the pidfile, in
`${XDG_STATE_HOME:-~/.local/state}/busywatch/busywatch.log`. If the marks
never appear, look there first.

The pidfile deliberately does not live in `HERDR_PLUGIN_STATE_DIR`. herdr
injects that variable into plugin commands but not into panes, so a `--stop`
that you run yourself from a shell looks somewhere the startup hook never wrote.

Two plugin actions, **Restart busywatch** and **Stop busywatch**, are available
from the workspace menu. By hand:

```bash
bin/busywatch-start [--restart|--stop]
```

`readlink ~/.local/state/busywatch/busywatch` prints the real path of the
plugin's `bin/busywatch`. That gives you the plugin root, which you need for
the command above and for Tuning. `herdr plugin list --json` reports it too, as
`plugin_root`. The plain `herdr plugin list` does not.

For a supervised alternative, [`systemd/busywatch.service`](systemd/busywatch.service)
runs the poller directly under systemd. Two pollers must not run at once, and
**Stop busywatch** signals only the poller in the pidfile, so the order matters.
Before you start, make sure that the plugin is enabled and started at least
once, so that the links exist.

```bash
# Stop busywatch from the workspace menu, or bin/busywatch-start --stop
herdr plugin disable busywatch
mkdir -p ~/.config/systemd/user
cp ~/.local/state/busywatch/busywatch.service ~/.config/systemd/user/
systemctl --user enable --now busywatch
```

It is a **user** unit. Installed system-wide, `%h` expands to `/root` and the
poller spins silently forever. Its `ExecStart` runs the poller through the
linked path and needs an edit in two cases. If you set `XDG_STATE_HOME` to
something other than `~/.local/state`, edit the path, because systemd does not
expand the variable. If an upgrade moves the plugin while the plugin is
disabled, nothing refreshes the link and the unit fails to start. Point it at
the new `bin/busywatch`.

## Tuning

### Programs that are not work

Some foreground programs mean "a human is sitting in a TUI", not "work to
wait on". busywatch ships a list of these: the shells, editors, pagers, file
managers, system monitors, `mpv`, `claude`, `herdr` and `ssh`. Nothing on it earns a
mark, so `ssh build-host 'make -j16'` shows nothing. Long-running servers are
deliberately **not** on it. They count as running, with a growing timer.

Edit the list in `~/.config/busywatch/ignore`. If you set `XDG_CONFIG_HOME`,
the file is `$XDG_CONFIG_HOME/busywatch/ignore` instead. Nothing creates the
directory for you, so `mkdir -p` it first. One name per line:

```
# a server is work, but a timer that grows all day is noise
caddy
# watch ssh sessions after all
-ssh
```

A bare name is ignored on top of the built-in list. `-name` takes a built-in
entry off it. `#` starts a comment, and a path is reduced to its last
component. The poller re-reads the file on its next full sweep, about ten
seconds, so no restart is needed. Each change to it leaves one line in the log
named under Running it, and an unchanged file leaves none. That is where to
look if an edit seems not to apply: no line means the poller saw no change.

Names are compared with the label busywatch would show, after the interpreter
rule: `node …/bin/cloudcli -p 8888` is labelled `cloudcli`, not `node`, so
`cloudcli` is the name to list. `sh` counts as an interpreter, so `sh build.sh`
is labelled `build.sh`. `bash`, `zsh`, `fish` and `dash` do not: `bash build.sh`
is labelled `bash` and is ignored with it. An ignored program leaves no mark of
any kind. The report the shell hook writes for it is dropped too, so a long
`vim` session never ends as `✓ vim`. The hook names the first word of the line,
though, so a script with a shebang is the one case where the two names differ:
`./run.sh` runs as `bash`, which is ignored, but ends as `✓ run.sh`. List
`run.sh` if that mark bothers you.

The built-in list is `IGNORE` at the top of `bin/busywatch`. It is the same
for every user, so edit the file, not the set.

### How much the row says

How much a Space mark carries is taste and sidebar width, so it lives in a
file, not in the code: `~/.config/busywatch/config`, beside the ignore list and
under `XDG_CONFIG_HOME` on the same terms.

```
# ~/.config/busywatch/config
names = on        # on (default) or off
run_slots = 3     # 1 to 9, how many $runN tokens get filled
```

One `key = value` per line, `#` starts a comment. It is re-read and logged on
the same terms as the ignore list above.

`names = off` takes every name off the Space row and leaves the glyphs, so each
mark has the shape the wait mark already has: the glyph alone for one pane, the
glyph and a count for more.

| | `names = on` | `names = off` |
| --- | --- | --- |
| one running | `▶ cargo` | `▶` |
| two running | `▶ 2` | `▶ 2` |
| one finished | `✓ cargo` | `✓` |
| two finished | `✓ 2` | `✓ 2` |

With names off the numbered slots stay empty, so their rows collapse and the
switch needs no layout change to go with it. The pane label and the tab bar are
not touched: the row is the surface short of width, and `▶ cargo 4m12s` on the
pane is the reason the row can afford to say less.

A line the poller does not understand costs that line and no more: the setting
keeps its default, and the log names the line, with the character that made it
unreadable shown as an escape when it is one you cannot see.

### Timing

At the top of `bin/busywatch` (see Running it for where that is):

- `MIN_BUSY_SECONDS`: how long a command must run before it counts. Keep the
  hook in step: export `BUSYWATCH_MIN_SECONDS` with the same value from your
  shell configuration. All three hooks read it from the environment.
- `POLL_SECONDS`, `TOKEN_TTL_MS`, `FULL_SWEEP_TICKS`: the trade between cost
  and latency.

## Limits

- Linux and macOS. `⏸` for shell panes reads `/proc/<pid>/syscall` and is
  Linux-only. That file sits behind a ptrace access check. The poller is a
  sibling of the pane's shell, not its parent, so `⏸` also needs
  `kernel.yama.ptrace_scope=0`. Some distributions ship `1`, which permits
  only a parent. Make sure of yours with `sysctl kernel.yama.ptrace_scope`. The
  rest is portable.
- The Space row has no elapsed time. At the default `ui.sidebar_width = 26`
  there is no room for a name *and* a duration. The duration is on the pane
  label instead. With names on, every mark on that row carries one, so a space
  with one command running and another finished spends its width on two names.
  Widen the sidebar, give a token a row of its own (see *A row per running
  command*), or set `names = off`.
- `✓` counts panes, not commands. Two commands that finish unseen in the same
  pane leave one mark, which names the last.
- Panes herdr already tracks keep its status. This means any pane with an
  `agent`, an `agent_session`, or an `agent_status` other than `unknown`, not
  only the ones that carry a session. Another source owns their lifecycle, so
  busywatch never labels them and never counts them in a workspace's `▶`
  roll-up. What it adds is the `⚙` token and a tab glyph: `▶` while Claude
  background work is in flight, `✓` when it finished unseen. Neither that tab
  glyph nor `⚙` has a matching Spaces row. The only agent-pane signal that
  reaches the Spaces panel is the `blocked` roll-up.

## Tests

```bash
python3 -m unittest discover tests
```

Stdlib only. The tests cover the pure helpers, drive the `Watcher` against a
stubbed socket, and write a report from each shell hook. zsh runs through a
real prompt cycle. bash runs over a pty for the appended-entry case and the
bash-preexec case, which is skipped when bash-preexec is not installed, and
calls the hook directly for the rest. fish runs through its event hook. The
poller against a real herdr server is not covered.

## Prior art

[robbyrussell/herdr-ohmyzsh](https://github.com/robbyrussell/herdr-ohmyzsh)
does the slow-command half from zsh hooks, inside a wider Oh My Zsh integration.
It reports commands as agents, so they share the Agents panel with real agents,
and it is zsh-only. busywatch keeps that panel for agents, works from any shell
or none, and adds the Claude background-work case.

## License

MIT
