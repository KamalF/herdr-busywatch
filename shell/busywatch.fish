# Report the exit status of slow commands to busywatch.
#
#   ln -s ~/.local/state/busywatch/busywatch.fish ~/.config/fish/conf.d/
#
# (Under $XDG_STATE_HOME/busywatch instead if you set that.) See
# shell/CONTRACT.md for what this writes and why.

function __busywatch_report --on-event fish_postexec
    set -l code $status
    test -n "$HERDR_PANE_ID"; or return
    # A comparison, not a command substitution: this runs after every command
    # and CONTRACT.md asks the fast path for comparisons (~85us -> ~45us).
    # `test -n` and not `set -q`, which is true for an exported-but-empty
    # value and would leave `math` and `test` printing after every command.
    set -l min 10
    test -n "$BUSYWATCH_MIN_SECONDS"; and set min $BUSYWATCH_MIN_SECONDS
    test "$CMD_DURATION" -ge (math "$min * 1000"); or return

    set -l dir (test -n "$XDG_CACHE_HOME"; and echo $XDG_CACHE_HOME; or echo $HOME/.cache)/busywatch
    # First non-blank word: a leading space (the keep-it-out-of-history
    # idiom) or a pasted indent would otherwise yield an empty name.
    set -l name (string match -r -- '\S+' $argv[1])
    test -n "$name"; or return
    # Everything external happens inside the child. fish reports both a failed
    # redirection and a command it cannot find on its *own* stderr, where no
    # redirection in this function can reach them, and the hook must stay
    # silent — so sh is the only external name here, the directory is created
    # inside it, and the basename is taken with a builtin. `command` so that a
    # shell function called sh cannot make the check and the call disagree.
    command -q sh; or return
    command sh -c 'mkdir -p "${3%/*}" && printf "%s\t%s\n" "$1" "$2" >"$3"' sh \
        $code (string replace -r -- '^.*/' '' $name) "$dir/$HERDR_PANE_ID" 2>/dev/null
end
