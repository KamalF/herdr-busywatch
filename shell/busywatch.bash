# Report the exit status of slow commands to busywatch.
#
#   source ~/.local/state/busywatch/busywatch.bash     # from ~/.bashrc, last
#
# (Under $XDG_STATE_HOME/busywatch instead if you set that.) See
# shell/CONTRACT.md for what this writes and why, and the README for what else
# may own your prompt: a DEBUG trap has one owner, and bash-preexec cannot be
# made to work with this at all.
#
# bash has no preexec, so the DEBUG trap stands in for one. It fires for every
# simple command, including the ones inside PROMPT_COMMAND, so a latch keeps
# only the first command after a prompt: __busywatch_precmd raises it and
# __busywatch_prompt_done, kept last in PROMPT_COMMAND, drops it once the
# prompt is drawn. Without the latch a PROMPT_COMMAND entry re-arms the timer,
# and the next command is reported with that entry's name and the time you
# spent sitting at the prompt.

__busywatch_prompt=1   # PROMPT_COMMAND has not finished, so ignore DEBUG

__busywatch_preexec() {
  # ${x-} throughout: under `set -u` a bare $x would abort the rc at source
  # time and then print before and after every command for the shell's life.
  [[ -n ${COMP_LINE-} ]] && return           # completion, not a real command
  [[ -n ${__busywatch_prompt-} ]] && return  # PROMPT_COMMAND, not your command
  [[ -n ${__busywatch_start-} ]] && return   # already timing this one
  __busywatch_start=$SECONDS
  __busywatch_cmd=${BASH_COMMAND%% *}
}

__busywatch_precmd() {
  local code=$?
  if [[ -n ${HERDR_PANE_ID-} && -n ${__busywatch_start-} ]]; then
    local seconds=$(( SECONDS - __busywatch_start ))
    if (( seconds >= ${BUSYWATCH_MIN_SECONDS:-10} )); then
      local dir=${XDG_CACHE_HOME:-$HOME/.cache}/busywatch
      # >| so noclobber cannot refuse the write, and the whole block silenced:
      # a hook that cannot write must not print. See CONTRACT.md.
      { mkdir -p "$dir" && printf '%s\t%s\n' "$code" "${__busywatch_cmd##*/}" \
          >| "$dir/$HERDR_PANE_ID"; } 2>/dev/null
    fi
  fi
  unset __busywatch_start
  __busywatch_prompt=1
  # Take both positions back, in case something was added since this file was
  # sourced. precmd must run FIRST — the status it captures is the status of
  # whatever ran immediately before it, so a prepended entry (direnv does
  # exactly this) would make every failure read as success. Anything that
  # *replaces* PROMPT_COMMAND outright takes both entries with it, and nothing
  # here can recover that.
  case ${PROMPT_COMMAND-} in
    __busywatch_precmd*) ;;
    *) PROMPT_COMMAND="__busywatch_precmd"$'\n'"${PROMPT_COMMAND//__busywatch_precmd/}" ;;
  esac
  case ${PROMPT_COMMAND-} in
    *__busywatch_prompt_done) ;;
    *) PROMPT_COMMAND="${PROMPT_COMMAND//$'\n'__busywatch_prompt_done/}"$'\n'"__busywatch_prompt_done"
       # bash took its copy of PROMPT_COMMAND before that rewrite, so this
       # cycle still releases the latch too early and would time an appended
       # entry instead of your command. Skip this cycle's release: one command
       # goes unmeasured, rather than one being reported under a wrong name.
       __busywatch_stale=1 ;;
  esac
  return $code
}

__busywatch_prompt_done() {
  if [[ -n ${__busywatch_stale-} ]]; then
    __busywatch_stale=
    return 0
  fi
  __busywatch_prompt=
}

trap '__busywatch_preexec' DEBUG
# Entries are joined with newlines, not ";". A neighbouring entry that is
# empty, blank, or already ends in ";" turns a ";" join into ";;", and bash
# then fails the whole of PROMPT_COMMAND with a syntax error at every prompt —
# taking your own entries down with it, unrecoverably. Values like that are
# real: /etc/profile.d/toolbox.sh ships PROMPT_COMMAND=" ".
case ${PROMPT_COMMAND-} in
  *__busywatch_precmd*) ;;
  *) PROMPT_COMMAND="__busywatch_precmd${PROMPT_COMMAND:+$'\n'$PROMPT_COMMAND}"$'\n'"__busywatch_prompt_done" ;;
esac
