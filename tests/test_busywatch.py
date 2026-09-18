"""Regression locks for the parts of busywatch that shipped wrong once.

    python3 -m unittest discover tests

Stdlib only, and no herdr server: the pure helpers directly, `Watcher` against
a stubbed socket, `busywatch-start`'s identity check against real processes,
and each shell hook in its own shell — zsh and bash through prompt cycles (bash
also over a pty) and fish through its event hook. Every case here is one that
shipped wrong or went untested at some point, so prefer a test that fails when
its fix is reverted over one that merely exercises the code.
"""
import builtins
import contextlib
import glob
import importlib.machinery
import importlib.util
import io
import json
import os
import pty
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name):
    """Import one of the bin/ scripts, which have no .py extension."""
    path = os.path.join(ROOT, "bin", name)
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


bw = load("busywatch")
bws = load("busywatch-start")
# Every Watcher() reads the ignore file, so point it away from the developer's
# own before any test constructs one. Panes gives itself a writable one. The
# object, not just its name, so the directory goes when the interpreter does.
IGNORE_DIR = tempfile.TemporaryDirectory()
bw.IGNORE_FILE = os.path.join(IGNORE_DIR.name, "ignore")


class Elapsed(unittest.TestCase):
    def test_units(self):
        self.assertEqual(bw.elapsed(0), "0s")
        self.assertEqual(bw.elapsed(59.9), "59s")
        self.assertEqual(bw.elapsed(60), "1m00s")
        self.assertEqual(bw.elapsed(605), "10m05s")
        self.assertEqual(bw.elapsed(3600), "1h00m")
        self.assertEqual(bw.elapsed(7565), "2h06m")


class StripGlyph(unittest.TestCase):
    def test_round_trip(self):
        for glyph in bw.TAB_GLYPHS:
            self.assertEqual(bw.strip_glyph(f"{glyph} build"), "build")
        self.assertEqual(bw.strip_glyph("build"), "build")

    def test_leaves_a_name_that_merely_starts_with_one(self):
        # No space, so it is part of the name, not a mark.
        self.assertEqual(bw.strip_glyph("✓done"), "✓done")

    def test_precedence_is_total(self):
        self.assertEqual(len(set(bw.TAB_GLYPHS.values())), len(bw.TAB_GLYPHS))


class Clean(unittest.TestCase):
    def test_strips_control_bytes(self):
        self.assertEqual(bw.clean("\x1b[2Jcargo\n"), "[2Jcargo")

    def test_bounds_length(self):
        self.assertEqual(len(bw.clean("a" * 500)), bw.LABEL_MAX)

    def test_keeps_ordinary_names(self):
        self.assertEqual(bw.clean("cargo"), "cargo")


class ScriptName(unittest.TestCase):
    def test_interpreter_gives_way_to_its_script(self):
        self.assertEqual(bw.script_name(["node", "/opt/bin/cloudcli", "-p", "8888"]),
                         "cloudcli")

    def test_skips_flags_and_env_assignments(self):
        self.assertEqual(bw.script_name(["env", "FOO=1", "cargo", "build"]), "cargo")

    def test_steps_over_a_subcommand(self):
        self.assertEqual(bw.script_name(["uv", "run", "pytest", "-x"]), "pytest")

    def test_takes_the_first_word_of_a_c_body(self):
        self.assertEqual(bw.script_name(["sh", "-c", "make -j8 && ./run"]), "make")

    def test_keeps_a_path_that_contains_a_space(self):
        # Only a -c body is word-split; splitting every argument chopped these
        # at the space and labelled the pane "My" / "Application".
        self.assertEqual(
            bw.script_name(["node", "/home/me/My Project/bin/cli.js"]), "cli.js")
        self.assertEqual(
            bw.script_name(["python3", "/me/Library/Application Support/t/run.py"]),
            "run.py")

    def test_no_program_at_all(self):
        self.assertIsNone(bw.script_name(["sh"]))
        self.assertIsNone(bw.script_name(["sh", "-c", "; :"]))


class Ignore(unittest.TestCase):
    """The per-user list edits the defaults; it does not replace them."""

    def test_no_text_leaves_the_defaults(self):
        self.assertEqual(bw.parse_ignore(""), bw.IGNORE)

    def test_a_bare_name_adds_and_a_dash_removes(self):
        names = bw.parse_ignore("# servers are noise here\ncaddy  # trailing comment\n"
                                "\n-ssh\n/usr/bin/tail\n- less\n")
        self.assertIn("caddy", names)
        self.assertIn("tail", names, "a path must be reduced to its basename")
        self.assertNotIn("ssh", names)
        self.assertNotIn("less", names, "whitespace after the dash is allowed")
        self.assertIn("vim", names, "the defaults must survive an edit")

    def test_the_defaults_are_never_mutated(self):
        bw.parse_ignore("-vim\nrogue\n")
        self.assertIn("vim", bw.IGNORE)
        self.assertNotIn("rogue", bw.IGNORE)

    def test_a_line_that_is_only_a_comment_or_a_dash_adds_nothing(self):
        # "foo/" too: its basename is empty, and "" must not join the set.
        self.assertEqual(bw.parse_ignore("#\n-\n   \nfoo/\n"), bw.IGNORE)

    def test_a_name_is_cleaned_like_the_label_it_must_match(self):
        # A label is cut at LABEL_MAX, so a longer name could never match.
        long = "x" * (bw.LABEL_MAX + 5)
        self.assertIn(bw.clean(long), bw.parse_ignore(long + "\n"))


class Foreground(unittest.TestCase):
    def info(self, argv):
        return {"process_info": {
            "foreground_process_group_id": 9, "shell_pid": 1,
            "foreground_processes": [{"pid": 9, "argv": argv}]}}

    def test_an_unprintable_argv_leaves_no_mark(self):
        # clean() has to run before the emptiness test, or the pane gets a
        # nameless "▶  1m00s" and the workspace token degrades to a bare count.
        self.addCleanup(setattr, bw, "api", bw.api)
        bw.api = lambda method, **kw: self.info(["\x1b\x1b\x07"])
        self.assertIsNone(bw.foreground("w1:p1"))

    def test_an_ordinary_argv_still_reports(self):
        self.addCleanup(setattr, bw, "api", bw.api)
        bw.api = lambda method, **kw: self.info(["/usr/bin/cargo", "build"])
        self.assertEqual(bw.foreground("w1:p1")["name"], "cargo")


class TakeExitReport(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.exit_dir = os.path.join(self.dir, "busywatch")
        os.makedirs(self.exit_dir)
        self.prev, bw.EXIT_DIR = bw.EXIT_DIR, self.exit_dir
        self.addCleanup(setattr, bw, "EXIT_DIR", self.prev)

    def write(self, pane_id, body):
        with open(os.path.join(self.exit_dir, pane_id), "w") as fh:
            fh.write(body)

    def test_reads_and_consumes(self):
        self.write("w1:p1", "7\tcargo\n")
        self.assertEqual(bw.take_exit_report("w1:p1"), (7, "cargo"))
        self.assertFalse(os.path.exists(os.path.join(self.exit_dir, "w1:p1")))

    def test_missing_file(self):
        self.assertIsNone(bw.take_exit_report("w1:p9"))

    def test_rejects_a_pane_id_that_is_not_one_component(self):
        victim = os.path.join(self.dir, "victim")
        with open(victim, "w") as fh:
            fh.write("7\tsecret\n")
        self.assertIsNone(bw.take_exit_report("../victim"))
        self.assertTrue(os.path.exists(victim), "an arbitrary file was deleted")

    def test_hostile_body_is_bounded_and_printable(self):
        self.write("w1:p1", "1\t\x1b[31m" + "A" * 500 + "\nsecond\n")
        _, name = bw.take_exit_report("w1:p1")
        self.assertLessEqual(len(name), bw.LABEL_MAX)
        self.assertTrue(name.isprintable())

    def test_garbage_status(self):
        self.write("w1:p1", "notanumber\tcargo\n")
        self.assertIsNone(bw.take_exit_report("w1:p1"))

    def test_rejects_a_status_outside_a_signed_32_bit_range(self):
        # 4300 digits parse fine as an int and went into a label unclipped.
        self.write("w1:p1", "9" * 4300 + "\tcargo\n")
        self.assertIsNone(bw.take_exit_report("w1:p1"))

    def test_keeps_a_wide_status_as_a_failure(self):
        # pwsh hands back a 32-bit code for a crashed native process. Dropping
        # the report would fall back to the poller's own view and show a tick.
        self.write("w1:p1", "-1073741819\tapp\n")
        self.assertEqual(bw.take_exit_report("w1:p1"), (-1073741819, "app"))

    def test_a_fifo_neither_blocks_the_tick_nor_gets_unlinked(self):
        fifo = os.path.join(self.exit_dir, "w2:p2")
        os.mkfifo(fifo)
        result = []

        def call():
            result.append(bw.take_exit_report("w2:p2"))

        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive(),
                         "a FIFO at the report path blocked the tick")
        self.assertEqual(result, [None])
        self.assertTrue(os.path.exists(fifo),
                        "a non-regular file was consumed and unlinked")

    def test_reads_only_the_first_line_of_a_bounded_prefix(self):
        self.write("w1:p1", "0\tcargo\n" + "x" * (4 * bw.REPORT_MAX))
        self.assertEqual(bw.take_exit_report("w1:p1"), (0, "cargo"))


class TaskRunning(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def transcript(self, name, records):
        path = os.path.join(self.dir, name)
        with open(path, "w") as fh:
            fh.writelines(json.dumps(record) + "\n" for record in records)
        return path

    @staticmethod
    def finished_turn(padding=0):
        return {"type": "assistant", "pad": "x" * padding,
                "message": {"stop_reason": "end_turn"}}

    def test_every_marker_a_real_task_file_can_end_with(self):
        # Literals, not a loop over BASH_DONE: iterating the constant means
        # deleting a marker deletes its own test case. All three occur in real
        # files under /tmp/claude-$UID/*/*/tasks/.
        for marker in ("[exited with code 0]", "[killed]",
                       "[process exited while detached; exit code unknown]"):
            path = os.path.join(self.dir, "b1.output")
            with open(path, "w") as fh:
                fh.write(f"building...\n{marker}\n")
            self.assertFalse(bw.task_running(path), marker)

    def test_a_marker_inside_the_output_is_not_the_end(self):
        # Containment over the whole tail counted a task as finished when its
        # own output merely mentioned a marker — a grep over a log, say.
        path = os.path.join(self.dir, "b1.output")
        with open(path, "w") as fh:
            fh.write("log.txt: [exited with code 0]\nstill working\n")
        self.assertTrue(bw.task_running(path))

    def test_bash_task_still_running(self):
        path = os.path.join(self.dir, "b1.output")
        with open(path, "w") as fh:
            fh.write("building...\n")
        self.assertTrue(bw.task_running(path))

    def test_finished_subagent(self):
        path = self.transcript("a1.output", [{"type": "user"}, self.finished_turn()])
        self.assertFalse(bw.task_running(path))

    def test_finished_subagent_with_a_record_larger_than_one_read(self):
        # The regression: a fixed 8KB window truncated the final record, which
        # then failed to parse and read as "still going" for 15 minutes. Real
        # transcripts routinely end with a record of tens of KB.
        path = self.transcript("a1.output",
                               [{"type": "user"}, self.finished_turn(64 * 1024)])
        self.assertGreater(os.path.getsize(path), 8192)
        self.assertFalse(bw.task_running(path))

    def test_running_subagent(self):
        path = self.transcript("a1.output", [self.finished_turn(), {"type": "user"}])
        self.assertTrue(bw.task_running(path))

    def test_transcript_caught_mid_write(self):
        path = self.transcript("a1.output", [self.finished_turn()])
        with open(path, "a") as fh:
            fh.write('{"type": "assist')
        self.assertTrue(bw.task_running(path))

    def test_single_record_file(self):
        path = self.transcript("a1.output", [self.finished_turn()])
        self.assertFalse(bw.task_running(path))

    def test_missing_file(self):
        self.assertFalse(bw.task_running(os.path.join(self.dir, "a9.output")))

    def test_a_fifo_in_the_task_directory_does_not_block_the_tick(self):
        # /tmp is shared, so the same guard take_exit_report has: without it a
        # planted FIFO blocks read_tail's open() and the poller stops dead.
        fifo = os.path.join(self.dir, "b1.output")
        os.mkfifo(fifo)
        result = []
        thread = threading.Thread(target=lambda: result.append(
            bw.task_running(fifo)), daemon=True)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "a FIFO blocked the task read")
        self.assertEqual(result, [False])

    def test_reading_a_directory_leaks_no_descriptor(self):
        # os.fdopen raises on a directory; an fd adopted only inside the `with`
        # leaked one per call, and the poller then silently stopped reporting
        # once it hit RLIMIT_NOFILE.
        if not os.path.isdir("/proc/self/fd"):
            self.skipTest("needs /proc to count descriptors")
        target = os.path.join(self.dir, "b1.output")
        os.makedirs(target)
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(20):
            bw.task_running(target)
        self.assertLessEqual(len(os.listdir("/proc/self/fd")), before + 1,
                             "read_tail leaked a descriptor")

    def test_a_record_of_the_wrong_shape_does_not_raise(self):
        # An AttributeError here escapes tick(), and it repeats every tick for
        # as long as the file stays inside TASK_STALE_SECONDS.
        for last in ("[]", "123", '"x"', '{"type":"assistant","message":"oops"}',
                     '{"type":"assistant","message":null}'):
            path = os.path.join(self.dir, "a1.output")
            with open(path, "w") as fh:
                fh.write('{"type": "user"}\n' + last + "\n")
            self.assertTrue(bw.task_running(path), last)


class Panes(unittest.TestCase):
    """Drive a Watcher against a stubbed api(); no herdr server needed."""

    def setUp(self):
        self.calls = []
        self.info = None
        self.blocked = set()
        self.panes = []
        self.addCleanup(setattr, bw, "api", bw.api)
        self.addCleanup(setattr, bw, "waiting_on_input", bw.waiting_on_input)
        bw.api = self.fake_api
        bw.waiting_on_input = lambda pid: pid in self.blocked
        # EXIT_DIR too: tick() runs sweep_reports(), which deletes every file
        # for a pane it does not see — against the real ~/.cache/busywatch that
        # discards a pending exit report for a command that just finished.
        self.exit_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.exit_dir, True)
        self.addCleanup(setattr, bw, "EXIT_DIR", bw.EXIT_DIR)
        bw.EXIT_DIR = self.exit_dir
        # IGNORE_FILE too, and not inside EXIT_DIR, where sweep_reports() would
        # delete it: the developer's own list must not shape a test.
        self.config_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.config_dir, True)
        self.addCleanup(setattr, bw, "IGNORE_FILE", bw.IGNORE_FILE)
        bw.IGNORE_FILE = os.path.join(self.config_dir, "ignore")
        # reload_ignore() logs to stdout, which here is the runner's. A test
        # that reads the log opens its own redirect inside this one.
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.w = bw.Watcher()
        self.w.swept = True  # skip the startup strip pass

    def fake_api(self, method, **params):
        self.calls.append((method, params))
        if method == "pane.process_info":
            return self.info
        if method == "pane.list":
            return {"panes": self.panes}
        return {}

    @staticmethod
    def pane(revision="r1", focused=False):
        return {"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1",
                "revision": revision, "focused": focused,
                "agent_status": "unknown"}

    @staticmethod
    def idle():
        """A pane at its prompt: the shell is its own foreground group."""
        return {"process_info": {"foreground_process_group_id": 100,
                                 "shell_pid": 100, "foreground_processes": []}}

    @staticmethod
    def process_info(argv, pids=(111,)):
        return {"process_info": {
            "foreground_process_group_id": 111, "shell_pid": 100,
            "foreground_processes": [
                {"pid": p, "argv": argv if p == 111 else ["helper"]} for p in pids]}}

    def titles(self):
        return [p.get("title") for m, p in self.calls
                if m == "pane.report_metadata" and "title" in p]

    def report(self, line):
        """What the shell hook leaves for pane w1:p1 when a slow command ends."""
        with open(os.path.join(self.exit_dir, "w1:p1"), "w") as fh:
            fh.write(line + "\n")

    def ignore_file(self, text):
        with open(bw.IGNORE_FILE, "w") as fh:
            fh.write(text)

    def tokens(self):
        """The last token patch reported for a workspace."""
        patches = [p["tokens"] for m, p in self.calls
                   if m == "workspace.report_metadata"]
        return patches[-1] if patches else None

    def shown(self, argv, label):
        """Poll pane w1:p1 once with this command long past the threshold."""
        self.info = self.process_info(argv)
        self.w.since["w1:p1"] = (label, -100.0)
        return self.w.shell_pane(self.pane("r1"), "w1:p1", 0.0, True)

    def test_a_dropped_process_info_reply_is_not_a_finished_command(self):
        self.info = self.process_info(["cargo", "build"])
        self.w.shell_pane(self.pane("r1"), "w1:p1", 0.0, True)      # clock starts
        self.assertEqual(self.w.shell_pane(self.pane("r2"), "w1:p1", 60.0, True),
                         ("run", "cargo"))
        self.info = None  # herdr stops answering for this pane
        self.calls.clear()
        self.assertEqual(self.w.shell_pane(self.pane("r3"), "w1:p1", 62.0, True),
                         ("run", "cargo"),
                         "an unreadable tick must not report the command finished")
        self.assertTrue(any(t.startswith("▶ cargo") for t in self.titles()),
                        "the label must be rewritten, or it expires on its TTL")

    def test_an_unreadable_tick_keeps_a_waiting_pane_waiting(self):
        # Replaying a remembered name as "run" turned a pane blocked on input
        # into a running one for as long as herdr stayed quiet.
        self.info = self.process_info(["./deploy.sh"], pids=(111,))
        self.blocked = {111}
        self.w.shell_pane(self.pane("r1"), "w1:p1", 0.0, True)
        self.assertEqual(self.w.shell_pane(self.pane("r2"), "w1:p1", 60.0, True),
                         ("wait", "deploy.sh"))
        self.info = None                       # herdr stops answering
        self.calls.clear()
        self.assertEqual(self.w.shell_pane(self.pane("r3"), "w1:p1", 62.0, True),
                         ("wait", "deploy.sh"),
                         "an unreadable tick turned a waiting pane into a runner")
        self.assertIn("⏸ deploy.sh", self.titles(),
                      "the pause glyph was replaced by a running label")

    def test_waiting_is_seen_on_a_child_of_the_group_leader(self):
        # ./deploy.sh sits in wait4 while the child it spawned reads the tty.
        self.info = self.process_info(["./deploy.sh"], pids=(111, 222))
        self.w.shell_pane(self.pane("r1"), "w1:p1", 0.0, True)
        self.blocked = {222}
        kind, name = self.w.shell_pane(self.pane("r2"), "w1:p1", 60.0, True)
        self.assertEqual((kind, name), ("wait", "deploy.sh"))

    def test_an_exit_report_with_no_name_leaves_no_mark(self):
        exit_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, exit_dir, True)
        self.addCleanup(setattr, bw, "EXIT_DIR", bw.EXIT_DIR)
        bw.EXIT_DIR = exit_dir
        with open(os.path.join(exit_dir, "w1:p1"), "w") as fh:
            fh.write("1\t\n")  # fish writes this for a leading-space command
        self.info = None
        self.w.revisions["w1:p1"] = "r1"
        kind, name = self.w.shell_pane(self.pane("r1"), "w1:p1", 10.0, False)
        self.assertEqual((kind, name), (None, None))
        self.assertNotIn("✗ None (1)", self.titles())
        self.assertEqual(self.w.done, {})

    def test_an_exit_report_for_an_ignored_program_leaves_no_mark(self):
        # The hook knows nothing of the ignore list, so a long vim session
        # ends with a report like any slow command's. Left alone, it became a
        # sticky "✓ vim" on a pane you had just left.
        self.report("0\tvim")
        self.info = None
        self.w.revisions["w1:p1"] = "r1"
        kind, name = self.w.shell_pane(self.pane("r1"), "w1:p1", 10.0, False)
        self.assertEqual((kind, name), (None, None))
        self.assertEqual(self.w.done, {})
        self.assertFalse(os.path.exists(os.path.join(self.exit_dir, "w1:p1")),
                         "the report must still be consumed")

    def test_a_report_naming_an_ignored_interpreter_keeps_the_scripts_name(self):
        # The hook writes the first word, `sh`; the poller showed build.sh.
        # Filtering the hook's word alone dropped the mark, exit status and
        # all, where HEAD had at least shown "✗ sh (2)".
        self.assertEqual(self.shown(["sh", "build.sh"], "build.sh"), ("run", "build.sh"))
        self.info = self.idle()
        self.report("2\tsh")
        self.assertEqual(self.w.shell_pane(self.pane("r2"), "w1:p1", 2.0, True),
                         ("failed", "build.sh"))
        self.assertEqual(self.w.done, {"w1:p1": ("build.sh", 2)})

    def test_a_report_that_lands_a_tick_after_the_prompt_still_names_the_script(self):
        # The hook writes milliseconds after the shell gets the terminal back,
        # so a poll can see the prompt first. Dropping the remembered name at
        # that poll left the pane "✓ build.sh" for a command that exited 2.
        self.shown(["sh", "build.sh"], "build.sh")
        self.info = self.idle()
        self.assertEqual(self.w.shell_pane(self.pane("r2"), "w1:p1", 2.0, True),
                         ("done", "build.sh"))       # no report yet
        self.report("2\tsh")
        self.assertEqual(self.w.shell_pane(self.pane("r3"), "w1:p1", 4.0, True),
                         ("failed", "build.sh"))

    def test_a_command_started_within_the_tick_does_not_name_the_last_ones_mark(self):
        # build.sh exits 2 and cargo is typed before the next poll: the poll
        # finds cargo, and the report for build.sh must not become "✗ cargo".
        self.shown(["sh", "build.sh"], "build.sh")
        self.info = self.process_info(["cargo", "build"])
        self.report("2\tsh")
        self.assertEqual(self.w.shell_pane(self.pane("r2"), "w1:p1", 2.0, True),
                         ("failed", "build.sh"))
        self.assertEqual(self.w.seen.get("w1:p1"), "cargo",
                         "the name of the command running now must be kept")

    def test_a_remembered_name_serves_one_report_only(self):
        # herdr stays silent for the whole of a second run, `python deploy.py`,
        # so the poller never sees it. Its report must not be marked with the
        # name remembered from the first run: "python" is a poor label, but
        # "build.sh" is a wrong one.
        self.shown(["sh", "build.sh"], "build.sh")
        self.info = self.idle()
        self.report("0\tsh")
        self.assertEqual(self.w.shell_pane(self.pane("r2"), "w1:p1", 2.0, True),
                         ("done", "build.sh"))
        self.w.done.clear()  # the pane was focused in between
        self.report("2\tpython")
        self.w.revisions["w1:p1"] = "r3"  # not polled this tick
        self.assertEqual(self.w.shell_pane(self.pane("r3"), "w1:p1", 60.0, False),
                         ("failed", "python"))

    def test_an_interpreter_name_is_kept_when_nothing_better_is_known(self):
        # The poller was down for the whole run: "✗ python (1)" is a poor
        # label, but no label at all hides a failure.
        self.report("1\tpython")
        self.w.revisions["w1:p1"] = "r1"
        self.assertEqual(self.w.shell_pane(self.pane("r1"), "w1:p1", 10.0, False),
                         ("failed", "python"))

    def test_an_ignored_script_run_through_an_interpreter_leaves_no_mark(self):
        # The README tells the user to list the label, "cloudcli", but the
        # hook reports "node". The poller's name has to decide.
        self.ignore_file("cloudcli\n")
        self.w.reload_ignore()
        self.assertEqual(self.shown(["node", "/opt/bin/cloudcli", "-p", "8888"], "cloudcli"),
                         (None, None))
        self.info = self.idle()
        self.report("0\tnode")
        self.assertEqual(self.w.shell_pane(self.pane("r2"), "w1:p1", 60.0, True),
                         (None, None))
        self.assertEqual(self.w.done, {})

    def test_a_fifo_at_the_ignore_path_neither_blocks_nor_is_read(self):
        self.ignore_file("caddy\n")
        self.w.reload_ignore()
        os.remove(bw.IGNORE_FILE)
        os.mkfifo(bw.IGNORE_FILE)
        out = io.StringIO()

        def call():  # a blocking open() on the FIFO would never return
            with contextlib.redirect_stdout(out):
                self.w.reload_ignore()

        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "a FIFO at the ignore path blocked the tick")
        self.assertEqual(self.w.ignore, bw.IGNORE, "a non-file puts the defaults back")
        self.assertIn("not a regular file", out.getvalue())

    def test_a_directory_at_the_ignore_path_is_named_in_the_log(self):
        # "mkdir -p it first", misread. A fresh poller found nothing to say
        # about it, and the README sends the user to the log.
        os.mkdir(bw.IGNORE_FILE)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            watcher = bw.Watcher()
        self.assertEqual(watcher.ignore, bw.IGNORE)
        self.assertIn("not a regular file", out.getvalue())

    def test_an_unreadable_file_keeps_the_defaults_and_says_so(self):
        self.ignore_file("caddy\n")
        os.chmod(bw.IGNORE_FILE, 0)
        if os.access(bw.IGNORE_FILE, os.R_OK):
            self.skipTest("running as root, where every file is readable")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.w.reload_ignore()
        self.assertNotIn("caddy", self.w.ignore)
        self.assertIn("not read", out.getvalue())
        self.assertNotIn("after reading", out.getvalue())
        # The fix the log asks for is a chmod, which changes neither mtime nor
        # size. It has to be noticed all the same.
        os.chmod(bw.IGNORE_FILE, 0o644)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.w.reload_ignore()
        self.assertIn("caddy", self.w.ignore, "a permission fix went unnoticed")
        self.assertIn("after reading", out.getvalue())

    def test_emptying_or_removing_the_file_is_applied_and_logged(self):
        self.ignore_file("caddy\n")
        self.w.reload_ignore()
        self.assertIn("caddy", self.w.ignore)
        self.ignore_file("")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.w.reload_ignore()
        self.assertNotIn("caddy", self.w.ignore)
        self.assertIn("after reading", out.getvalue(),
                      "the README sends the user to the log for every edit")
        os.remove(bw.IGNORE_FILE)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.w.reload_ignore()
        self.assertEqual(self.w.ignore, bw.IGNORE)
        self.assertIn("does not exist", out.getvalue())

    def test_an_unchanged_file_is_not_logged_again(self):
        self.ignore_file("caddy\n")
        self.w.reload_ignore()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.w.reload_ignore()
        self.assertEqual(out.getvalue(), "", "one line per change, none per sweep")

    def test_an_edit_to_the_ignore_file_is_applied_without_a_restart(self):
        self.info = self.process_info(["caddy", "run"])
        self.panes = [self.pane("r1")]
        self.w.since["w1:p1"] = ("caddy", -100.0)  # long past the threshold
        self.w.tick()                                 # tick 1: a full sweep
        self.assertIn("w1:p1", self.w.shown, "caddy is watched by default")
        self.ignore_file("caddy\n")
        for _ in range(bw.FULL_SWEEP_TICKS - 1):
            self.w.tick()                             # not yet a full sweep
        self.assertIn("w1:p1", self.w.shown, "only a full sweep re-reads the file")
        self.w.tick()                                 # the next full sweep
        self.assertIn("caddy", self.w.ignore)
        self.assertEqual(self.w.shown, {})
        self.assertEqual(self.w.done, {}, "newly ignored is not finished unseen")

    def test_an_unchanged_idle_pane_is_polled_once_per_full_sweep(self):
        self.panes = [self.pane("r1")]
        for _ in range(12):
            self.w.tick()
        polls = sum(1 for m, _ in self.calls if m == "pane.process_info")
        self.assertEqual(polls, 3,
                         f"12 ticks at FULL_SWEEP_TICKS={bw.FULL_SWEEP_TICKS} "
                         "should be 3 full sweeps (ticks 1, 6, 11)")

    def test_a_failed_startup_sweep_is_retried(self):
        # swept set before the loop would abandon the rest of the strip pass on
        # any error, and main() now swallows the exception — so a glyph left by
        # a crashed poller would stay on those tabs for good.
        calls = []

        def exploding_api(method, **params):
            calls.append(method)
            if method == "tab.list":
                raise RuntimeError("herdr blew up mid-sweep")
            return {}

        bw.api = exploding_api
        watcher = bw.Watcher()
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                watcher.mark_tabs({}, ["w1"])
            self.assertFalse(watcher.swept, "the sweep did not finish")
        self.assertEqual(calls.count("tab.list"), 2, "the sweep must be retried")

    def test_an_unanswered_tab_list_leaves_the_sweep_unfinished(self):
        # api() returns None for a timeout or an error reply; treating that as
        # an empty workspace would mark the strip pass done and never revisit
        # those tabs, leaving a crashed poller's glyphs up for the whole run.
        bw.api = lambda method, **params: None if method == "tab.list" else {}
        watcher = bw.Watcher()
        watcher.mark_tabs({}, ["w1"])
        self.assertFalse(watcher.swept, "an unanswered tab.list is not a sweep")

    def test_a_stranded_glyph_stays_in_the_marked_set(self):
        """An unanswered tab.list must not drop the workspace from tabs_marked.

        The tab name has no TTL, so whatever holds a glyph has to be revisited;
        dropping it leaves the glyph on for the life of the poller, and
        shutdown() will not strip it either because it drives off tabs_marked.
        """
        answered = {"wA": True, "wB": True}
        listed = []

        def api(method, **params):
            if method == "tab.list":
                workspace = params["workspace_id"]
                listed.append(workspace)
                if not answered[workspace]:
                    return None
                return {"tabs": [{"tab_id": f"{workspace}:t1", "label": "1"}]}
            return {}

        bw.api = api
        watcher = bw.Watcher()
        watcher.mark_tabs({"wA:t1": "▶", "wB:t1": "▶"}, ["wA", "wB"])
        self.assertEqual(watcher.tabs_marked, {"wA:t1", "wB:t1"})
        answered["wB"] = False          # herdr stops answering for wB
        listed.clear()
        watcher.mark_tabs({"wA:t1": "▶"}, ["wA", "wB"])   # wB no longer wanted
        self.assertIn("wB:t1", watcher.tabs_marked,
                      "a glyph on wB was stranded with nothing to strip it")
        answered["wB"] = True
        listed.clear()
        watcher.mark_tabs({}, [])
        self.assertIn("wB", listed, "wB was never revisited")

    def test_a_glyph_interrupted_mid_rename_is_still_recorded(self):
        # The rename round-trip is where the loop spends its time, so the
        # record has to be made before it, not after.
        def api(method, **params):
            if method == "tab.list":
                return {"tabs": [{"tab_id": "wA:t1", "label": "1"}]}
            if method == "tab.rename":
                raise RuntimeError("SIGTERM landed in the round-trip")
            return {}

        bw.api = api
        watcher = bw.Watcher()
        watcher.swept = True
        with self.assertRaises(RuntimeError):
            watcher.mark_tabs({"wA:t1": "▶"}, ["wA"])
        self.assertIn("wA:t1", watcher.tabs_marked,
                      "a glyph the server had already applied was forgotten")

    def test_a_malformed_tab_entry_does_not_raise(self):
        """mark_tabs runs inside the SIGTERM handler, where nothing may raise.

        An exception there unwinds into main()'s `except Exception`, which logs
        and loops — so the poller would ignore that and every later SIGTERM.
        """
        renamed = []

        def api(method, **params):
            if method == "tab.list":
                return {"tabs": [
                    "not a dict",
                    {"label": "build"},                    # no tab_id
                    {"tab_id": 7, "label": "x"},           # tab_id not a str
                    {"tab_id": "wA:t4", "label": None},    # label not a str
                    {"tab_id": "wA:t5", "label": "▶ ok"},  # the only usable one
                ]}
            if method == "tab.rename":
                renamed.append(params["label"])
            return {}

        bw.api = api
        watcher = bw.Watcher()
        watcher.swept = True
        watcher.tabs_marked = {"wA:t5"}
        watcher.mark_tabs({}, ["wA"])
        self.assertEqual(renamed, ["ok"], "the one good entry was not stripped")

    def test_tabs_is_not_assumed_to_be_a_list(self):
        # A number, not a string: a string is iterable, so the per-entry guard
        # would already have covered it and this would prove nothing.
        bw.api = lambda method, **params: (
            {"tabs": 7} if method == "tab.list" else {})
        watcher = bw.Watcher()
        watcher.swept = True
        watcher.tabs_marked = {"wA:t1"}
        watcher.mark_tabs({}, ["wA"])          # must not raise

    def test_a_closed_tab_leaves_the_marked_set(self):
        # A closed tab appears in no tab.list answer, so the incremental
        # discard cannot reach it: it would hold its workspace in `touched`
        # and cost a round-trip every tick for the life of the poller.
        listed = []

        def api(method, **params):
            if method == "tab.list":
                listed.append(params["workspace_id"])
                return {"tabs": [{"tab_id": "wA:t1", "label": "1"}]}
            return {}

        bw.api = api
        watcher = bw.Watcher()
        watcher.swept = True
        watcher.tabs_marked = {"wA:t1", "wA:t2"}     # t2 has since been closed
        watcher.mark_tabs({"wA:t1": "▶"}, ["wA"])
        self.assertEqual(watcher.tabs_marked, {"wA:t1"},
                         "a closed tab stayed in the marked set")
        listed.clear()
        watcher.mark_tabs({}, ["wA"])
        watcher.mark_tabs({}, ["wA"])
        self.assertEqual(listed, ["wA"],
                         f"the workspace was still being listed: {listed}")

    def test_a_closed_workspace_is_not_retried_forever(self):
        # api() reports "closed" and "did not answer" the same way, so only the
        # live set tells them apart. Retrying a closed workspace costs a socket
        # round-trip every tick for the life of the poller.
        listed = []

        def api(method, **params):
            if method == "tab.list":
                listed.append(params["workspace_id"])
                if params["workspace_id"] == "wA":
                    return None          # closed: herdr answers with an error
                return {"tabs": [{"tab_id": "wB:t1", "label": "1"}]}
            return {}

        bw.api = api
        watcher = bw.Watcher()
        watcher.swept = True
        watcher.tabs_marked = {"wA:t1"}
        watcher.mark_tabs({}, ["wB"])    # wA is gone from the live set
        self.assertNotIn("wA:t1", watcher.tabs_marked,
                         "a closed workspace stayed in the marked set")
        listed.clear()
        watcher.mark_tabs({}, ["wB"])
        self.assertNotIn("wA", listed, "a closed workspace was listed again")

    def test_one_unanswered_tab_list_does_not_reopen_the_startup_sweep(self):
        # self.swept must latch. Recomputing it would re-enter the first-pass
        # branch and re-run the blunt strip pass, which takes a leading glyph
        # off a tab the user named that way.
        answered = [True]
        listed = []

        def api(method, **params):
            if method == "tab.list":
                listed.append(params["workspace_id"])
                return {"tabs": []} if answered[0] else None
            return {}

        bw.api = api
        watcher = bw.Watcher()
        watcher.mark_tabs({}, ["wA", "wB"])
        self.assertTrue(watcher.swept)
        answered[0] = False
        watcher.tabs_marked = {"wB:t1"}
        watcher.mark_tabs({}, ["wA", "wB"])
        self.assertTrue(watcher.swept, "a transient failure reopened the sweep")
        answered[0] = True
        listed.clear()
        watcher.mark_tabs({}, ["wA", "wB"])
        self.assertNotIn("wA", listed, "the whole sweep ran again")

    def test_a_completed_sweep_is_not_repeated(self):
        calls = []

        def api(method, **params):
            calls.append(method)
            return {"tabs": []} if method == "tab.list" else {}

        bw.api = api
        watcher = bw.Watcher()
        watcher.mark_tabs({}, ["w1"])
        self.assertTrue(watcher.swept)
        watcher.mark_tabs({}, ["w1"])
        self.assertEqual(calls.count("tab.list"), 1, "the sweep ran twice")

    def test_shutdown_withdraws_every_kind_of_mark(self):
        # The TTL would drop all but the tab glyph eventually; shutdown makes
        # it immediate, and it must go through the same writers the ticks use.
        cleared = []

        def api(method, **params):
            if method == "tab.list":
                return {"tabs": []}
            cleared.append((method, params))
            return {}

        bw.api = api
        watcher = bw.Watcher()
        watcher.swept = True
        watcher.labelled.add("w1:p1")
        watcher.bg_marked.add("w1:p2")
        watcher.marked.add("w1")
        watcher.shutdown()
        self.assertIn(("pane.report_metadata",
                       {"pane_id": "w1:p1", "source": bw.SOURCE,
                        "clear_title": True}), cleared)
        self.assertIn(("pane.report_metadata",
                       {"pane_id": "w1:p2", "source": bw.SOURCE,
                        "tokens": {"bg": None}}), cleared)
        self.assertTrue(any(m == "workspace.report_metadata" for m, _ in cleared))

    def test_shutdown_strips_a_marked_tab_even_before_a_full_sweep(self):
        # The tab glyph is the one mark with no TTL, so SIGTERM has to take it
        # off. shutdown() passes no workspaces, so the first-pass branch must
        # add to the marked set rather than replace it.
        renames = []

        def api(method, **params):
            if method == "tab.list":
                return {"tabs": [{"tab_id": "w1:t1", "label": "▶ build"}]}
            if method == "tab.rename":
                renames.append(params["label"])
            return {}

        bw.api = api
        watcher = bw.Watcher()
        watcher.swept = False          # a tab.list went unanswered earlier
        watcher.tabs_marked = {"w1:t1"}
        watcher.shutdown()
        self.assertEqual(renames, ["build"], "the glyph was left on the tab")

    def test_sweep_reports_removes_only_files_for_panes_that_are_gone(self):
        exit_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, exit_dir, True)
        self.addCleanup(setattr, bw, "EXIT_DIR", bw.EXIT_DIR)
        bw.EXIT_DIR = exit_dir
        for pane_id in ("w1:p1", "w9:p9"):
            with open(os.path.join(exit_dir, pane_id), "w") as fh:
                fh.write("0\tcargo\n")
        self.w.sweep_reports({"w1:p1"})
        self.assertEqual(os.listdir(exit_dir), ["w1:p1"])

    def test_a_full_sweep_drops_reports_for_panes_that_are_gone(self):
        exit_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, exit_dir, True)
        self.addCleanup(setattr, bw, "EXIT_DIR", bw.EXIT_DIR)
        bw.EXIT_DIR = exit_dir
        with open(os.path.join(exit_dir, "w9:p9"), "w") as fh:
            fh.write("0\tcargo\n")   # a pane that no longer exists
        self.panes = [self.pane("r1")]
        for _ in range(6):            # at least one full sweep
            self.w.tick()
        self.assertEqual(os.listdir(exit_dir), [],
                         "tick() must run the sweep, not just define it")

    def test_one_finished_pane_names_the_space_mark(self):
        # The mark is sticky because you were not there to see the command
        # end. A mark that says only "something failed" drops the one fact
        # you came back for.
        self.w.done["w1:p1"] = ("pytest", 1)
        self.panes = [self.pane("r1")]
        self.info = self.idle()
        self.w.tick()
        self.assertEqual(self.tokens()["done"], "✗ pytest")

    def test_a_finished_pane_that_succeeded_is_named_too(self):
        self.w.done["w1:p1"] = ("cargo", 0)
        self.panes = [self.pane("r1")]
        self.info = self.idle()
        self.w.tick()
        self.assertEqual(self.tokens()["done"], "✓ cargo")

    def test_two_finished_panes_keep_the_count(self):
        # Two names do not fit the row, and neither one is the answer.
        self.w.workspace_tokens("w1", [], 0, ["pytest", "make"], failed=True)
        self.assertEqual(self.tokens()["done"], "✗ 2")


class Background(unittest.TestCase):
    """Watcher.background_count and the Claude agent-row mark."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.addCleanup(setattr, bw, "TASK_ROOT", bw.TASK_ROOT)
        bw.TASK_ROOT = self.dir
        self.tasks = os.path.join(self.dir, "project", "sess", "tasks")
        os.makedirs(self.tasks)
        self.w = bw.Watcher()

    def task(self, name, body):
        path = os.path.join(self.tasks, name)
        with open(path, "w") as fh:
            fh.write(body)
        return path

    def test_the_project_directory_is_resolved_once(self):
        # The obvious glob puts a * where the project directory goes, which
        # re-lists all of TASK_ROOT on every call — measured 3.4ms against
        # 0.02ms, once per Claude pane per tick.
        self.task("b1.output", "building...\n")
        wild = []
        real = glob.glob
        self.addCleanup(setattr, bw.glob, "glob", real)
        bw.glob.glob = lambda pattern: wild.append(pattern) or real(pattern)
        for _ in range(4):
            self.w.background_count("sess")
        self.assertEqual(sum("/*/" in p for p in wild), 1,
                         f"the project directory was re-scanned: {wild}")

    def test_two_sessions_do_not_evict_each_others_verdicts(self):
        # self.verdicts is shared across sessions, so pruning it to the paths
        # one session saw made two idle Claude panes re-read every settled
        # file — including transcripts, which read in windows up to 1 MiB.
        other = os.path.join(self.dir, "project", "other", "tasks")
        os.makedirs(other)
        for path in (os.path.join(self.tasks, "b1.output"),
                     os.path.join(other, "b1.output")):
            with open(path, "w") as fh:
                fh.write("building...\n")
        reads = []
        real = bw.task_running
        self.addCleanup(setattr, bw, "task_running", real)
        bw.task_running = lambda p: reads.append(p) or real(p)
        for _ in range(3):
            self.w.background_count("sess")
            self.w.background_count("other")
        self.assertEqual(len(self.w.verdicts), 2,
                         "the sessions evicted each other")
        self.assertEqual(len(reads), 2,
                         f"a settled file was re-read: {len(reads)} reads")

    def test_a_session_with_no_tasks_directory_is_remembered(self):
        # The wildcard glob is what the memo exists to avoid, and a session
        # that has never backgrounded anything is the common case, not a
        # corner: 2 of the 5 most recent sessions on this machine have no
        # tasks/ directory.
        wild = []
        real = glob.glob
        self.addCleanup(setattr, bw.glob, "glob", real)
        bw.glob.glob = lambda pattern: wild.append(pattern) or real(pattern)
        for _ in range(4):
            self.assertEqual(self.w.background_count("nosuch"), 0)
        self.assertEqual(sum("/*/" in p for p in wild), 1,
                         f"the wildcard glob was paid again: {wild}")

    def test_a_full_sweep_looks_again_for_a_missing_tasks_directory(self):
        # Memoizing None must not be permanent, or a first background task
        # would never be noticed.
        self.assertEqual(self.w.background_count("later"), 0)
        self.assertIn("later", self.w.task_dirs)
        self.addCleanup(setattr, bw, "api", bw.api)
        bw.api = lambda method, **kw: {"panes": []} if method == "pane.list" else {}
        self.w.sweep = 0            # the next tick is a full one
        self.w.tick()
        self.assertNotIn("later", self.w.task_dirs,
                         "a missing tasks directory was memoized for good")

    def test_the_verdict_cache_does_not_outlive_its_files(self):
        path = self.task("b1.output", "building...\n")
        self.assertEqual(self.w.background_count("sess"), 1)
        self.assertIn(path, self.w.verdicts)
        os.remove(path)
        self.w.background_count("sess")
        self.assertEqual(self.w.verdicts, {},
                         "the cache kept an entry for a file that is gone")

    def test_a_finished_task_marks_the_pane_until_it_is_focused(self):
        # claude_pane's one rule: a focused pane, or one with work still in
        # flight, has nothing finished to report.
        pane = {"agent_session": {"agent": "claude", "kind": "id", "value": "sess"},
                "agent_status": "idle", "focused": False}
        path = self.task("b1.output", "building...\n")
        self.addCleanup(setattr, bw, "api", bw.api)
        bw.api = lambda method, **kw: {}
        self.assertEqual(self.w.claude_pane(pane, "w1:p1"), "▶")   # in flight
        with open(path, "w") as fh:
            fh.write("done\n[exited with code 0]\n")
        self.assertEqual(self.w.claude_pane(pane, "w1:p1"), "✓")   # finished unseen
        self.assertEqual(self.w.claude_pane(pane, "w1:p1"), "✓")   # and sticky
        pane["focused"] = True
        self.assertIsNone(self.w.claude_pane(pane, "w1:p1"),
                          "looking at the pane did not clear the mark")

    def test_work_in_flight_again_clears_a_finished_mark(self):
        pane = {"agent_session": {"agent": "claude", "kind": "id", "value": "sess"},
                "agent_status": "idle", "focused": False}
        path = self.task("b1.output", "done\n[exited with code 0]\n")
        self.addCleanup(setattr, bw, "api", bw.api)
        bw.api = lambda method, **kw: {}
        self.w.bg_count["w1:p1"] = 1          # something was running last tick
        self.assertEqual(self.w.claude_pane(pane, "w1:p1"), "✓")
        with open(path, "w") as fh:
            fh.write("building again...\n")
        self.assertEqual(self.w.claude_pane(pane, "w1:p1"), "▶")
        # The glyph is "▶" either way while a task runs, so assert the state:
        # a stale "finished" left here resurfaces the moment the task ends.
        self.assertEqual(self.w.bg_done, set(),
                         "a new task did not clear the finished mark")


class Replies(unittest.TestCase):
    """A faulty peer on the socket must cost a tick, not the poller."""

    def serve(self, body):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "herdr.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(1)
        self.addCleanup(server.close)

        def once():
            try:
                conn, _ = server.accept()
                conn.recv(65536)
                conn.sendall(body)
                conn.close()
            except OSError:
                pass

        threading.Thread(target=once, daemon=True).start()
        self.addCleanup(setattr, bw, "SOCKET_PATH", bw.SOCKET_PATH)
        bw.SOCKET_PATH = path
        return path

    def test_valid_json_of_the_wrong_shape_is_not_a_result(self):
        for body in (b"5\n", b"[1,2]\n", b'"text"\n'):
            self.serve(body)
            self.assertIsNone(bw.api("pane.list"), body)

    def test_list_panes_survives_a_result_that_is_not_a_mapping(self):
        self.serve(b'{"result": "oops"}\n')
        self.assertEqual(bw.list_panes(), [])

    def test_list_panes_drops_entries_missing_the_fields_tick_needs(self):
        self.addCleanup(setattr, bw, "api", bw.api)
        bw.api = lambda method, **kw: {"panes": [
            "not a dict", {"pane_id": "w1:p1"},
            {"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1"}]}
        self.assertEqual(len(bw.list_panes()), 1)

    def test_sigterm_exits_even_if_withdrawing_the_marks_fails(self):
        """A failure in shutdown() must not make the poller ignore SIGTERM.

        The handler runs inside tick(), so its exception unwinds into main()'s
        `except Exception`, which logs and loops. `--stop` has already removed
        the pidfile by then, so nothing could find the process again.
        """
        poller = os.path.join(ROOT, "bin", "busywatch")
        script = (
            "import importlib.machinery, importlib.util, os, signal, threading, time\n"
            f"loader = importlib.machinery.SourceFileLoader('bw', {poller!r})\n"
            "spec = importlib.util.spec_from_loader('bw', loader)\n"
            "bw = importlib.util.module_from_spec(spec)\n"
            "loader.exec_module(bw)\n"
            # a tick slow enough for the signal to land inside it
            "bw.list_panes = lambda: (time.sleep(5), [])[1]\n"
            "bw.Watcher.shutdown = lambda self: (_ for _ in ()).throw("
            "RuntimeError('withdrawing failed'))\n"
            "threading.Timer(0.2, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()\n"
            "bw.main()\n")
        try:
            proc = subprocess.run([sys.executable, "-c", script],
                                  capture_output=True, timeout=25, check=False)
        except subprocess.TimeoutExpired:
            self.fail("the poller ignored SIGTERM after shutdown() failed")
        # Assert the outcome, not just that it finished: otherwise this passes
        # whenever the child exits early for any reason, including never
        # reaching main() at all.
        self.assertEqual(proc.returncode, 0,
                         f"the child did not exit cleanly: {proc.stderr!r}")

    def test_tick_does_not_swallow_its_own_exceptions(self):
        # main() is what keeps the poller alive (see the test below); tick()
        # must let the failure out so that guard is the only one.
        watcher = bw.Watcher()
        self.addCleanup(setattr, bw, "list_panes", bw.list_panes)
        bw.list_panes = lambda: (_ for _ in ()).throw(RuntimeError("bad reply"))
        with self.assertRaises(RuntimeError):
            watcher.tick()          # main() is what swallows it

    def test_the_poller_survives_a_tick_that_raises(self):
        """main() must swallow a failed tick, or one bad reply kills the poller.

        Driven through main() itself with a tick that always raises. Asserting
        on the source text, or feeding it a malformed reply, both pass even
        with main()'s guard deleted — the first because the text is still
        there, the second because the shape guards mean nothing raises.
        """
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        poller = os.path.join(ROOT, "bin", "busywatch")
        wrapper = os.path.join(directory, "wrapper.py")
        with open(wrapper, "w") as fh:
            fh.write(
                "import importlib.machinery, importlib.util\n"
                f"loader = importlib.machinery.SourceFileLoader('bw', {poller!r})\n"
                "spec = importlib.util.spec_from_loader('bw', loader)\n"
                "bw = importlib.util.module_from_spec(spec)\n"
                "loader.exec_module(bw)\n"
                "bw.POLL_SECONDS = 0.05\n"
                "def boom(self):\n"
                "    raise RuntimeError('bad reply')\n"
                "bw.Watcher.tick = boom\n"
                "bw.main()\n")
        log = os.path.join(directory, "poller.log")
        with open(log, "w") as sink:
            proc = subprocess.Popen([sys.executable, wrapper], stdout=sink,
                                    stderr=subprocess.STDOUT, text=True)
        self.addCleanup(proc.wait)     # LIFO: wait runs after kill
        self.addCleanup(proc.kill)
        time.sleep(1.5)   # ~30 ticks
        self.assertIsNone(proc.poll(),
                          "a tick that raises must cost a tick, not the poller")


class Poller(unittest.TestCase):
    """bin/busywatch-start's identity check, which gates a SIGTERM."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.addCleanup(setattr, bws, "STATE", bws.STATE)
        self.addCleanup(setattr, bws, "PIDFILE", bws.PIDFILE)
        bws.STATE = self.dir  # main() links into it
        bws.PIDFILE = os.path.join(self.dir, "busywatch.pid")

    def write_pid(self, pid):
        with open(bws.PIDFILE, "w") as fh:
            fh.write(str(pid))

    def test_a_pid_that_means_a_process_group_never_reaches_os_kill(self):
        # os.kill(0, …) signals our whole group and os.kill(-1, …) everything
        # we may signal, so these must be refused before the call, not after.
        seen = []
        self.addCleanup(setattr, bws.os, "kill", bws.os.kill)
        bws.os.kill = lambda pid, sig: seen.append((pid, sig))
        for pid in (0, -1, 1):
            self.write_pid(pid)
            self.assertIsNone(bws.running(), f"pid {pid} was accepted")
        self.assertEqual(seen, [], f"os.kill was called with {seen}")

    def test_a_process_merely_naming_the_poller_is_not_the_poller(self):
        proc = subprocess.Popen(["tail", "-f", bws.POLLER],
                                stdout=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        time.sleep(0.2)          # let exec finish; `tail -f` never exits
        self.assertFalse(bws.is_poller(proc.pid),
                         "a pager holding the path passed the identity check")

    def test_an_unknown_argument_does_not_start_a_poller(self):
        # A typo'd flag used to fall through to the start branch and spawn one.
        started = []
        self.addCleanup(setattr, bws, "start", bws.start)
        bws.start = lambda: started.append(True) or 4242
        self.addCleanup(setattr, sys, "argv", sys.argv)

        sys.argv = ["busywatch-start", "--stopp"]
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bws.main(), 2)
        self.assertEqual(started, [], "a bad flag started a poller")

        sys.argv = ["busywatch-start"]      # and the guard is not over-broad
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bws.main(), 0)
        self.assertEqual(started, [True])

    def test_a_real_poller_is_recognised(self):
        # The mirror of the negative tests: without this, a change that made
        # is_poller always answer False would pass the suite, turning every
        # start into a second poller and every --stop into "not running".
        proc = subprocess.Popen(["tail", bws.POLLER, "-f"],
                                stdout=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        time.sleep(0.2)
        self.assertTrue(bws.is_poller(proc.pid),
                        "argv[1] is the poller path, as start() spawns it")

    def test_a_real_poller_is_recognised_without_proc(self):
        # macOS has no /proc, so ps is the only probe there. `yes <path>` has
        # the poller's own shape — one argument and nothing after it — which
        # is what distinguishes it from a pager holding the same path.
        proc = subprocess.Popen(["yes", bws.POLLER],
                                stdout=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        time.sleep(0.2)
        def no_proc(path, *args, **kwargs):
            if str(path).startswith("/proc/"):
                raise OSError("no /proc here")
            return builtins.open(path, *args, **kwargs)

        # A module global shadows the builtin for lookups inside the module.
        bws.open = no_proc
        self.addCleanup(delattr, bws, "open")
        self.assertTrue(bws.is_poller(proc.pid), "the ps fallback must agree")

    def test_the_ps_fallback_survives_a_path_containing_a_space(self):
        """Splitting ps output into fields cuts a path at its first space.

        Linux never reaches this branch while /proc answers, but macOS has no
        /proc at all: there, a mis-read means `--stop` always says "not
        running" and every server start leaves another poller behind.
        """
        spaced = "/home/me/My Plugins/busywatch/bin/busywatch"
        self.addCleanup(setattr, bws, "POLLER", bws.POLLER)
        bws.POLLER = spaced

        def ps_says(line):
            def run(*a, **k):
                return subprocess.CompletedProcess(a, 0, stdout=line)
            bws.subprocess.run = run

        self.addCleanup(setattr, bws.subprocess, "run", bws.subprocess.run)

        def no_proc(path, *args, **kwargs):
            if str(path).startswith("/proc/"):
                raise OSError("no /proc here")
            return builtins.open(path, *args, **kwargs)

        bws.open = no_proc
        self.addCleanup(delattr, bws, "open")

        ps_says(f"/usr/bin/python3 {spaced}\n".encode())
        self.assertTrue(bws.is_poller(4242), "the real poller was rejected")
        # A space can fall on either side of the line. This is the case the
        # first reading cannot handle: the interpreter path holds the space,
        # so splitting on it lands in the middle of the interpreter.
        interp_dir = os.path.join(self.dir, "my py")
        os.makedirs(interp_dir)
        interpreter = os.path.join(interp_dir, "python3")
        with open(interpreter, "w") as fh:
            fh.write("")
        ps_says(f"{interpreter} {spaced}\n".encode())
        self.assertTrue(bws.is_poller(4242),
                        "an interpreter path containing a space was rejected")
        ps_says(f"/usr/bin/tail -f {spaced}\n".encode())
        self.assertFalse(bws.is_poller(4242), "a pager passed as the poller")
        ps_says(b"")
        self.assertFalse(bws.is_poller(4242), "empty ps output passed")

    def test_a_non_utf8_cmdline_is_read_without_falling_back_to_ps(self):
        """The /proc read must be binary, not text.

        A text read raises UnicodeDecodeError, which is a ValueError and so is
        swallowed by the same `except` — the verdict then comes from `ps`, and
        on a host without `ps` from the `return True` fallback, which is the
        false "already running" (and the wrong SIGTERM) this check exists to
        prevent. So assert that `ps` is never reached.
        """
        cmdline = os.path.join(self.dir, "cmdline")
        with open(cmdline, "wb") as fh:
            fh.write(b"/usr/bin/python3\x00\xff\xfe/not-the-poller\x00")

        def redirect(path, *args, **kwargs):
            return builtins.open(cmdline if str(path).startswith("/proc/")
                                 else path, *args, **kwargs)

        consulted = []
        bws.open = redirect
        self.addCleanup(delattr, bws, "open")
        self.addCleanup(setattr, bws.subprocess, "run", bws.subprocess.run)
        bws.subprocess.run = lambda *a, **k: consulted.append(a) or (_ for _ in ()).throw(
            AssertionError("ps was consulted; the /proc read must have raised"))
        self.assertFalse(bws.is_poller(4242))
        self.assertEqual(consulted, [])


class Links(unittest.TestCase):
    """The fixed paths the README tells you to source and to enable."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.addCleanup(setattr, bws, "STATE", bws.STATE)
        bws.STATE = self.dir

    def assert_linked(self):
        for rel in bws.LINKED:
            link = os.path.join(self.dir, os.path.basename(rel))
            self.assertTrue(os.path.exists(link), f"{link} is missing or dangling")
            self.assertEqual(os.path.realpath(link),
                             os.path.realpath(os.path.join(ROOT, rel)))
        self.assertEqual(sorted(os.listdir(self.dir)),
                         sorted(os.path.basename(rel) for rel in bws.LINKED),
                         "a leftover is a link nobody sources and nothing removes")

    def test_a_link_left_by_an_older_plugin_root_is_repointed(self):
        # What makes the README's literal path safe across an upgrade that
        # moves the plugin: the link follows the root, it is not written once.
        os.symlink("/gone/busywatch/shell/busywatch.zsh",
                   os.path.join(self.dir, "busywatch.zsh"))
        bws.link_files()
        self.assert_linked()

    def test_a_start_that_finds_a_poller_running_still_links(self):
        # The startup hook runs on every server start and nearly always finds
        # the poller alive, so linking only inside start() left an upgraded
        # install with no links until an explicit --restart.
        self.addCleanup(setattr, bws, "running", bws.running)
        self.addCleanup(setattr, sys, "argv", sys.argv)
        bws.running = lambda: 4242
        sys.argv = ["busywatch-start"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bws.main(), 0)
        self.assert_linked()


class Socket(unittest.TestCase):
    def test_a_dribbling_peer_cannot_hold_a_request_open(self):
        """A per-recv timeout is not a deadline: bytes with no newline reset it.

        The buffer cap alone would let this run for hours, wedging the tick.
        """
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "herdr.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(1)
        self.addCleanup(server.close)

        def dribble():
            conn = None
            try:
                conn, _ = server.accept()
                conn.recv(65536)
                while True:  # one byte at a time, never a newline
                    conn.sendall(b"x")
                    time.sleep(0.02)
            except OSError:
                pass
            finally:
                if conn is not None:
                    conn.close()

        threading.Thread(target=dribble, daemon=True).start()
        self.addCleanup(setattr, bw, "SOCKET_PATH", bw.SOCKET_PATH)
        self.addCleanup(setattr, bw, "REQUEST_TIMEOUT", bw.REQUEST_TIMEOUT)
        bw.SOCKET_PATH = path
        bw.REQUEST_TIMEOUT = 0.3   # the deadline is the thing under test
        # In a thread, so a regression fails the test instead of hanging it:
        # without the deadline this call runs until the 1MB cap, for hours.
        result = []
        caller = threading.Thread(target=lambda: result.append(bw.api("pane.list")),
                                  daemon=True)
        caller.start()
        caller.join(5)
        self.assertFalse(caller.is_alive(),
                         "api() must give up on its own deadline")
        self.assertIsNone(result[0])


class Sandbox(unittest.TestCase):
    """The suite must not touch the developer's own busywatch state."""

    def test_no_test_deletes_a_real_exit_report(self):
        # tick() runs sweep_reports(), which deletes the report of every pane
        # it does not see. A Watcher test that forgets to redirect EXIT_DIR
        # therefore empties ~/.cache/busywatch — discarding the exit status of
        # a command that had just finished, for a pane that really exists.
        cache = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cache, True)
        os.makedirs(os.path.join(cache, "busywatch"))
        sentinel = os.path.join(cache, "busywatch", "wZ:p9")
        with open(sentinel, "w") as fh:
            fh.write("0\tcargo\n")
        proc = subprocess.run([sys.executable, "-m", "unittest", "-v",
                                "tests.test_busywatch.Panes"],
                               cwd=ROOT,
                               env=dict(os.environ, XDG_CACHE_HOME=cache),
                               capture_output=True, text=True,
                               timeout=120, check=False)
        # The child has to have actually run, or this guard passes on a
        # sentinel nothing ever touched — which is how it would stop guarding
        # the moment the class is renamed.
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertGreater(proc.stderr.count(" ... ok"), 3, proc.stderr)
        self.assertTrue(os.path.exists(sentinel),
                        "a Watcher test deleted a real pending exit report")


class Hooks(unittest.TestCase):
    """Each hook must actually write a report. busywatch.zsh once never did."""

    MIN = "1"

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        # XDG_CONFIG_HOME too: fish reads conf.d/* even non-interactively, and
        # the developer's own busywatch.fish and atuin handlers would join in.
        self.env = dict(os.environ, HERDR_PANE_ID="w1:p1", XDG_CACHE_HOME=self.dir,
                        XDG_CONFIG_HOME=os.path.join(self.dir, "config"),
                        BUSYWATCH_MIN_SECONDS=self.MIN)
        self.env.pop("PROMPT_COMMAND", None)

    @property
    def report(self):
        path = os.path.join(self.dir, "busywatch", "w1:p1")
        with open(path) as fh:
            return fh.read()

    def run_shell(self, shell, script, *args):
        # The resolved path, not the name: a test that thins PATH would
        # otherwise not find the shell itself, since subprocess searches the
        # env it is given.
        path = shell if os.path.isabs(shell) else shutil.which(shell)
        if path is None:
            self.skipTest(f"{shell} is not installed")
        proc = subprocess.run([path, *args], input=script, env=self.env,
                              timeout=60, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, check=False)
        return proc.stdout

    def test_fish(self):
        hook = os.path.join(ROOT, "shell", "busywatch.fish")
        self.run_shell("fish", f"""
            source {hook}
            set -g CMD_DURATION 2000
            false
            emit fish_postexec "cargo build --release"
        """, "--no-config")
        self.assertEqual(self.report, "1\tcargo\n")

    def test_fish_treats_an_empty_min_seconds_as_unset(self):
        # `set -q VAR[1]` is true for an exported-but-empty value, which left
        # `math` and `test` printing after every command and writing nothing —
        # the one rule CONTRACT.md calls hard.
        hook = os.path.join(ROOT, "shell", "busywatch.fish")
        self.env["BUSYWATCH_MIN_SECONDS"] = ""
        out = self.run_shell("fish", f"""
            source {hook}
            set -g CMD_DURATION 20000
            false
            emit fish_postexec "cargo build"
        """, "--no-config")
        self.assertEqual(out.strip(), "",
                         f"the hook printed for an empty threshold: {out!r}")
        self.assertEqual(self.report, "1\tcargo\n")

    def test_fish_ignores_a_leading_space(self):
        # A leading space (the keep-it-out-of-history idiom) or a pasted indent
        # used to yield an empty name, which the poller drew as "None".
        hook = os.path.join(ROOT, "shell", "busywatch.fish")
        self.run_shell("fish", f"""
            source {hook}
            set -g CMD_DURATION 2000
            emit fish_postexec "  /usr/bin/sleep 12"
        """, "--no-config")
        self.assertEqual(self.report, "0\tsleep\n")

    def test_fish_is_silent_when_sh_is_missing(self):
        # The report is written by a child sh; without a presence check the
        # hook prints fish's command-not-found after every slow command.
        hook = os.path.join(ROOT, "shell", "busywatch.fish")
        thin = os.path.join(self.dir, "bin")
        os.makedirs(thin)
        fish = shutil.which("fish")
        if fish is None:
            self.skipTest("fish is not installed")
        self.env["PATH"] = thin      # so the hook cannot find sh
        # --no-config: fish sources /usr/share/fish/vendor_conf.d/* whatever
        # XDG_CONFIG_HOME says, and on this distro one of those scripts shells
        # out to `cat`, which the thin PATH does not have. The hook is sourced
        # explicitly here, so conf.d is not needed.
        out = self.run_shell(fish, f"""
            source {hook}
            set -g CMD_DURATION 2000
            emit fish_postexec "cargo build"
        """, "--no-config")
        self.assertEqual(out.strip(), "",
                         f"the hook printed with no sh on PATH: {out!r}")
        self.assertFalse(os.path.exists(
            os.path.join(self.dir, "busywatch", "w1:p1")))

    def test_fish_needs_nothing_on_path_but_sh(self):
        """Every other external is reached from inside the child sh.

        fish reports a command it cannot find on its own stderr, where the
        hook's redirections cannot reach it, so each external name in the
        function body is a way for the hook to start printing.
        """
        hook = os.path.join(ROOT, "shell", "busywatch.fish")
        thin = os.path.join(self.dir, "bin")
        os.makedirs(thin)
        for tool in ("sh", "mkdir"):  # mkdir is resolved by sh, not by fish
            found = shutil.which(tool)
            if found is None:
                self.skipTest(f"{tool} is not installed")
            os.symlink(found, os.path.join(thin, tool))
        fish = shutil.which("fish")
        if fish is None:
            self.skipTest("fish is not installed")
        self.env["PATH"] = thin
        out = self.run_shell(fish, f"""
            source {hook}
            set -g CMD_DURATION 2000
            false
            emit fish_postexec "/usr/bin/cargo build"
        """, "--no-config")
        self.assertEqual(out.strip(), "",
                         f"the hook printed with only sh on PATH: {out!r}")
        self.assertEqual(self.report, "1\tcargo\n")

    def test_fish_is_silent_when_it_cannot_write(self):
        # fish reports a failed redirection on its own stderr, which no
        # redirection inside the function can catch — hence the child shell.
        hook = os.path.join(ROOT, "shell", "busywatch.fish")
        self.unwritable_cache()
        out = self.run_shell("fish", f"""
            source {hook}
            set -g CMD_DURATION 2000
            emit fish_postexec "cargo build"
        """, "--no-config")
        self.assertEqual(out.strip(), "",
                         f"the hook printed on a failed write: {out!r}")

    def test_zsh(self):
        hook = os.path.join(ROOT, "shell", "busywatch.zsh")
        # A real prompt cycle: preexec, the command, then precmd. Driving the
        # functions by hand would re-fire preexec and reset the clock.
        zdotdir = os.path.join(self.dir, "zdot")
        os.makedirs(zdotdir)
        with open(os.path.join(zdotdir, ".zshrc"), "w") as fh:
            fh.write(f"source {hook}\n")
        self.env["ZDOTDIR"] = zdotdir
        # -i: precmd and preexec only fire for an interactive shell.
        # --no-globalrcs: /etc/zshrc may register precmd hooks of its own.
        self.run_shell("zsh", "sleep 2\n(exit 7)\nsleep 2; (exit 7)\nexit\n",
                       "-i", "--no-globalrcs")
        self.assertEqual(self.report, "7\tsleep\n")

    def test_bash(self):
        hook = os.path.join(ROOT, "shell", "busywatch.bash")
        self.run_shell("bash", f"""
            source {hook}
            __busywatch_prompt=
            __busywatch_start=$(( SECONDS - 5 ))
            __busywatch_cmd=/usr/bin/cargo
            (exit 7)
            __busywatch_precmd
        """)
        self.assertEqual(self.report, "7\tcargo\n")

    def test_bash_ignores_prompt_command_entries(self):
        # The regression: the DEBUG trap re-armed inside PROMPT_COMMAND, so the
        # hook timed the wait at the prompt and named a PROMPT_COMMAND entry.
        hook = os.path.join(ROOT, "shell", "busywatch.bash")
        self.env["PROMPT_COMMAND"] = "history -a"
        out = self.run_shell("bash", f"""
            source {hook}
            __busywatch_prompt_done          # the prompt is drawn
            __busywatch_precmd               # PROMPT_COMMAND runs again
            history -a
            __busywatch_preexec              # the DEBUG trap fires for it
            test -z "$__busywatch_start" && echo NOT-ARMED
        """)
        # Asserting on NOT-ARMED, not just on the absence of a report file: with
        # the latch removed nothing is written either, because no time passed.
        self.assertIn("NOT-ARMED", out,
                      "the latch must keep PROMPT_COMMAND out of the timer")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "busywatch", "w1:p1")))

    def test_bash_never_reports_an_appended_prompt_command_entry(self):
        """The repair of PROMPT_COMMAND lands a cycle late, so the cycle that
        triggers it must not time the appended entry instead of your command.
        """
        hook = os.path.join(ROOT, "shell", "busywatch.bash")
        main, settle_until = self.interactive_bash(
            f"PS1='P> '\n__bw_appended() {{ :; }}\nsource {hook}\n"
            'PROMPT_COMMAND="$PROMPT_COMMAND;__bw_appended"\n')
        reports = []
        for _ in range(2):
            os.write(main, b"sleep 2; false\n")
            settle_until(4.0, "P> ")   # the prompt is back: precmd has run
            path = os.path.join(self.dir, "busywatch", "w1:p1")
            if os.path.exists(path):
                with open(path) as fh:
                    reports.append(fh.read().strip())
                os.remove(path)
            else:
                reports.append("")
        self.assertNotIn("__bw_appended", " ".join(reports),
                         f"an appended PROMPT_COMMAND entry was timed: {reports}")
        self.assertEqual(reports[1], "1\tsleep",
                         f"the cycle after the repair must work: {reports}")

    def interactive_bash(self, rc_text):
        """An interactive bash on a pty, with `rc_text` as its rc file.

        Returns the pty master and a `settle_until(seconds, marker)` that
        drains output until `marker` shows up or the time is spent. The DEBUG
        trap and PROMPT_COMMAND only run for real in interactive mode, so the
        prompt-cycle regressions cannot be locked any other way.
        """
        if shutil.which("bash") is None:
            self.skipTest("bash is not installed")
        rc = os.path.join(self.dir, "bashrc")
        with open(rc, "w") as fh:
            # HISTFILE= : an `exit` would otherwise append the test's commands
            # to the developer's real ~/.bash_history.
            fh.write("HISTFILE=\n" + rc_text)
        main, worker = pty.openpty()
        proc = subprocess.Popen(["bash", "--rcfile", rc, "-i"], stdin=worker,
                                stdout=worker, stderr=worker, env=self.env,
                                start_new_session=True)
        os.close(worker)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.addCleanup(os.close, main)

        def settle_until(seconds, marker):
            end = time.time() + seconds
            seen = []
            while time.time() < end:
                if select.select([main], [], [], 0.1)[0]:
                    try:
                        seen.append(os.read(main, 65536).decode(errors="replace"))
                    except OSError:
                        break
                    if marker in "".join(seen):
                        break
            return "".join(seen)

        settle_until(2.0, "P> ")      # the first prompt: the rc has run
        return main, settle_until

    def test_bash_registers_with_bash_preexec(self):
        """Next to bash-preexec the hook must use its arrays, not fight it.

        bash-preexec (atuin embeds it) takes both ends of PROMPT_COMMAND,
        which on bash 5.1+ becomes an array the hook's string tests cannot
        see, and the DEBUG trap or PS0. Fighting it released the latch early
        and reported a prompt-hook command as yours.
        """
        preexec = os.environ.get("BASH_PREEXEC",
                                 os.path.expanduser("~/.bash-preexec.sh"))
        if not os.path.exists(preexec):
            self.skipTest("bash-preexec not found: set BASH_PREEXEC to a copy of"
                          " https://github.com/rcaloras/bash-preexec")
        hook = os.path.join(ROOT, "shell", "busywatch.bash")
        # A stand-in for atuin: a prompt hook that runs commands of its own,
        # which is exactly what the DEBUG trap would otherwise time.
        main, settle_until = self.interactive_bash(
            f"PS1='P> '\nsource {preexec}\n"
            "__fake_atuin_precmd() { /bin/true; command sleep 0; }\n"
            "__fake_atuin_preexec() { /bin/true; }\n"
            "precmd_functions+=(__fake_atuin_precmd)\n"
            "preexec_functions+=(__fake_atuin_preexec)\n"
            f"source {hook}\n")
        path = os.path.join(self.dir, "busywatch", "w1:p1")

        os.write(main, b"sh -c 'sleep 2; exit 7'\n")
        settle_until(4.0, "P> ")
        self.assertEqual(self.report, "7\tsh\n")
        os.remove(path)

        # A group's first word is `{`; the name must be the command inside.
        os.write(main, b"{ sh -c 'sleep 2; exit 5'; }\n")
        settle_until(4.0, "P> ")
        self.assertEqual(self.report, "5\tsh\n")
        os.remove(path)

        # Sit at the prompt past the threshold, then fail fast: the wait must
        # not be timed and the prompt hook's commands must not be named.
        time.sleep(1.5)
        os.write(main, b"false\n")
        settle_until(2.0, "P> ")
        self.assertFalse(os.path.exists(path),
                         "a fast command was reported after an idle prompt")

        # And the hook installed nothing of its own: the reports above prove
        # it registered, and how bash-preexec hooks preexec is its own affair.
        os.write(main, b"declare -p PROMPT_COMMAND; trap -p DEBUG\n")
        out = settle_until(2.0, "P> ")
        os.write(main, b"exit\n")
        self.assertNotIn("__busywatch", out,
                         f"the hook touched PROMPT_COMMAND or the trap: {out!r}")

    def unwritable_cache(self):
        """A cache dir the hook cannot write into, restored on teardown."""
        readonly = os.path.join(self.dir, "readonly")
        os.makedirs(os.path.join(readonly, "busywatch"))
        os.chmod(os.path.join(readonly, "busywatch"), 0o500)
        self.addCleanup(os.chmod, os.path.join(readonly, "busywatch"), 0o700)
        self.env["XDG_CACHE_HOME"] = readonly
        if os.access(os.path.join(readonly, "busywatch"), os.W_OK):
            self.skipTest("running as root: mode 0500 is not a barrier")

    @staticmethod
    def complaints(out):
        """Lines that are the hook's fault, for a shell that prints on its own.

        Only zsh needs this: `zsh -i` emits prompt bytes and `%` partial-line
        markers of its own, so its output is never exactly empty. bash's is.
        """
        return [l for l in out.splitlines()
                if "__busywatch" in l or "denied" in l or "cannot" in l
                or "No space" in l or "error" in l.lower()]

    def test_zsh_is_silent_when_it_cannot_write(self):
        # CONTRACT: a hook that cannot write must not print. zsh reports a
        # failed redirection through the function that ran it.
        hook = os.path.join(ROOT, "shell", "busywatch.zsh")
        zdotdir = os.path.join(self.dir, "zdot")
        os.makedirs(zdotdir)
        with open(os.path.join(zdotdir, ".zshrc"), "w") as fh:
            fh.write(f"PS1=''\nsource {hook}\n")
        self.env["ZDOTDIR"] = zdotdir
        self.unwritable_cache()
        out = self.run_shell("zsh", "sleep 2\n(exit 7)\nexit\n",
                             "-i", "--no-globalrcs")
        self.assertEqual(self.complaints(out), [],
                         f"the zsh hook printed on a failed write: {out!r}")

    def test_zsh_is_silent_under_nounset(self):
        # `setopt nounset` turns a bare $VAR into an error at every prompt, for
        # the life of the shell. bash's hook guards this with ${x-}; zsh's did
        # not, and printed `__busywatch_start: parameter not set`.
        hook = os.path.join(ROOT, "shell", "busywatch.zsh")
        zdotdir = os.path.join(self.dir, "zdot-u")
        os.makedirs(zdotdir)
        with open(os.path.join(zdotdir, ".zshrc"), "w") as fh:
            fh.write(f"setopt nounset\nPS1=''\nsource {hook}\n")
        self.env["ZDOTDIR"] = zdotdir
        out = self.run_shell("zsh", "\nsleep 2\n(exit 7)\nexit\n",
                             "-i", "--no-globalrcs")
        self.assertEqual(self.complaints(out), [],
                         f"the zsh hook printed under nounset: {out!r}")

    def test_zsh_is_silent_under_nounset_without_the_datetime_module(self):
        # zmodload is allowed to fail, so EPOCHSECONDS may not exist; under
        # `setopt nounset` a bare expansion of it would print before every
        # command for the life of the shell.
        hook = os.path.join(ROOT, "shell", "busywatch.zsh")
        zdotdir = os.path.join(self.dir, "zdot-nomod")
        os.makedirs(zdotdir)
        with open(os.path.join(zdotdir, ".zshrc"), "w") as fh:
            fh.write("setopt nounset\nPS1=''\n"
                     "zmodload() { return 1 }\n"      # pretend zsh/datetime is absent
                     f"source {hook}\n")
        self.env["ZDOTDIR"] = zdotdir
        out = self.run_shell("zsh", "\nsleep 2\n(exit 7)\nexit\n",
                             "-i", "--no-globalrcs")
        self.assertEqual(self.complaints(out), [],
                         f"the zsh hook printed with no datetime module: {out!r}")

    def test_bash_is_silent_when_it_cannot_write(self):
        hook = os.path.join(ROOT, "shell", "busywatch.bash")
        self.unwritable_cache()
        out = self.run_shell("bash", f"""
            source {hook}
            __busywatch_prompt=
            __busywatch_start=$(( SECONDS - 5 ))
            __busywatch_cmd=/usr/bin/cargo
            (exit 7)
            __busywatch_precmd
        """)
        # Exactly empty, not merely free of known complaints: bash prints
        # nothing at all in the passing case, so anything at all is a defect.
        self.assertEqual(out.strip(), "",
                         f"the bash hook printed on a failed write: {out!r}")

    def test_bash_writes_through_noclobber(self):
        # `>` refuses an existing file under noclobber and prints; `>|` does not.
        hook = os.path.join(ROOT, "shell", "busywatch.bash")
        os.makedirs(os.path.join(self.dir, "busywatch"))
        with open(os.path.join(self.dir, "busywatch", "w1:p1"), "w") as fh:
            fh.write("stale\n")
        out = self.run_shell("bash", f"""
            set -o noclobber
            source {hook}
            __busywatch_prompt=
            __busywatch_start=$(( SECONDS - 5 ))
            __busywatch_cmd=/usr/bin/cargo
            (exit 7)
            __busywatch_precmd
        """)
        self.assertEqual(out.strip(), "", out)
        self.assertEqual(self.report, "7\tcargo\n")

    def test_bash_takes_back_the_first_position_in_prompt_command(self):
        # precmd captures $? , so it must run first: direnv's integration
        # prepends to PROMPT_COMMAND, which would hand it direnv's status.
        hook = os.path.join(ROOT, "shell", "busywatch.bash")
        out = self.run_shell("bash", f"""
            source {hook}
            PROMPT_COMMAND="_other_hook;$PROMPT_COMMAND"
            __busywatch_precmd
            echo "PC=$PROMPT_COMMAND"
        """)
        line = next(l for l in out.splitlines() if l.startswith("PC="))
        self.assertTrue(line[len("PC="):].startswith("__busywatch_precmd"), line)

    def test_bash_keeps_the_latch_release_last_in_prompt_command(self):
        # Anything appended to PROMPT_COMMAND after sourcing would otherwise
        # run after the latch was released and re-arm the timer at the prompt.
        hook = os.path.join(ROOT, "shell", "busywatch.bash")
        out = self.run_shell("bash", f"""
            source {hook}
            PROMPT_COMMAND="$PROMPT_COMMAND;history -a"
            __busywatch_precmd
            echo "PC=${{PROMPT_COMMAND//$'\n'/|}}"
        """)
        # Entries are newline-joined, so flatten before asserting on the order.
        line = next(l for l in out.splitlines() if l.startswith("PC="))
        self.assertTrue(line.endswith("__busywatch_prompt_done"), line)


if __name__ == "__main__":
    unittest.main()
