# Report the exit status of slow commands to busywatch.
#
#   source ~/.local/state/busywatch/busywatch.zsh      # from ~/.zshrc
#
# (Under $XDG_STATE_HOME/busywatch instead if you set that.) See
# shell/CONTRACT.md for what this writes and why.

zmodload -F zsh/datetime p:EPOCHSECONDS 2>/dev/null

__busywatch_preexec() {
  # ${x-} here too: zmodload is allowed to fail, and under `setopt nounset` a
  # bare $EPOCHSECONDS would then print before every command. Empty disables
  # the hook silently, which is the contract.
  __busywatch_start=${EPOCHSECONDS-}
  __busywatch_cmd=${${(z)1}[1]}
}

__busywatch_precmd() {
  local code=$?
  # ${x-} so `setopt nounset` cannot make this print at every prompt.
  [[ -n ${HERDR_PANE_ID-} && -n ${__busywatch_start-} ]] || return 0
  local seconds=$(( EPOCHSECONDS - __busywatch_start ))
  unset __busywatch_start
  (( seconds >= ${BUSYWATCH_MIN_SECONDS:-10} )) || return 0
  local dir=${XDG_CACHE_HOME:-$HOME/.cache}/busywatch
  mkdir -p $dir 2>/dev/null || return 0
  # Silenced as a block: a hook that cannot write must not print.
  { print -r -- "$code	${__busywatch_cmd:t}" >| $dir/$HERDR_PANE_ID } 2>/dev/null
}

autoload -Uz add-zsh-hook
add-zsh-hook preexec __busywatch_preexec
add-zsh-hook precmd __busywatch_precmd
