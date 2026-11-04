"""Lines for one app, written between {{kit.only APP}} and {{kit.end}}."""

import unittest

import support  # noqa: F401  (puts the tool on the import path)
from agent_kit_sync import render, toml
from agent_kit_sync.checks import Checker


def values(app):
    return render.Values(home="/h", projects="/h/p", worktrees="/h/p/w", module="/h/m", app=app, os="linux", cli="x", hooks={"m/guard": "run guard"})


AGENT = """---
name: helper
description: Helps
claude.tools: Read
codex.sandbox_mode: read-only
---
First line.

{{kit.only claude}}
A hook runs `{{kit.hook m/guard}}` for this app only.

{{kit.end}}
Last line.
"""


START = "<!-- agent-kit-sync only claude -->\n"
END = "<!-- agent-kit-sync end only -->\n"


def carry(source, app, edit):
    """Render `source` for `app`, apply `edit` to the copy, and carry it back."""
    rendered = render.render_text(source, values(app)).text
    edited = edit(rendered)
    back = render.carry_back_text(source, rendered, edited, values(app))
    assert render.render_text(back, values(app)).text == edited
    return back


class OnlyBlockTests(unittest.TestCase):
    def test_section_lines_appear_only_in_their_app(self):
        source = "Shared.\n{{kit.only codex}}\nCodex only.\n{{kit.end}}\nAlso shared.\n"
        self.assertEqual(render.render_text(source, values("codex")).text,
                         "Shared.\n<!-- agent-kit-sync only codex -->\nCodex only.\n<!-- agent-kit-sync end only -->\nAlso shared.\n")
        self.assertEqual(render.render_text(source, values("claude")).text, "Shared.\nAlso shared.\n")

    def test_block_can_list_several_apps(self):
        source = "{{kit.only claude cursor}}\nNot Codex.\n{{kit.end}}\n"
        self.assertEqual(render.render_text(source, values("cursor")).text, "<!-- agent-kit-sync only claude cursor -->\nNot Codex.\n" + END)
        self.assertEqual(render.render_text(source, values("codex")).text, "")

    def test_agent_body_block_reaches_markdown_and_not_codex(self):
        claude = render.render_agent_md(AGENT, "claude", values("claude")).text
        self.assertIn(START + "A hook runs `run guard` for this app only.\n\n" + END, claude)
        self.assertNotIn("kit.", claude)
        codex = toml.loads(render.render_agent_codex(AGENT, values("codex")))
        self.assertEqual(codex["developer_instructions"], "First line.\n\nLast line.\n")
        self.assertEqual(codex["sandbox_mode"], "read-only")

    def test_edits_in_claude_copy_carry_back(self):
        for old, new in (("Last line.", "Final line."), ("for this app only.", "for Claude Code only.")):
            with self.subTest(old=old):
                rendered = render.render_agent_md(AGENT, "claude", values("claude")).text
                back = render.carry_back_agent_md(AGENT, "claude", rendered, rendered.replace(old, new), values("claude"))
                self.assertEqual(back, AGENT.replace(old, new))

    def test_codex_edit_keeps_the_other_apps_block(self):
        rendered = render.render_agent_codex(AGENT, values("codex"))
        back = render.carry_back_agent_codex(AGENT, rendered, rendered.replace("Last line.", "Final line."), values("codex"))
        self.assertEqual(back, AGENT.replace("Last line.", "Final line."))

    def test_a_line_added_on_either_side_of_a_comment_line_stays_there(self):
        source = "Intro.\n\n{{kit.only claude cursor}}\nA hook runs in a sandbox.\n{{kit.end}}\n\nOutro.\n"
        inside = carry(source, "claude", lambda t: t.replace("sandbox.\n", "sandbox.\nIt blocks writes.\n"))
        self.assertEqual(render.render_text(inside, values("codex")).text, "Intro.\n\n\nOutro.\n")
        self.assertIn("It blocks writes.", render.render_text(inside, values("cursor")).text)
        outside = carry(source, "claude", lambda t: t.replace("end only -->\n", "end only -->\nIt blocks writes.\n").replace("Outro.", "Outro, reworded."))
        self.assertEqual(render.render_text(outside, values("codex")).text, "Intro.\n\nIt blocks writes.\n\nOutro, reworded.\n")

    def test_codex_rewording_around_a_hidden_block_keeps_it_in_place(self):
        source = "Shared.\n{{kit.only claude}}\nClaude only.\n{{kit.end}}\nAlso shared.\n"
        back = carry(source, "codex", lambda t: "Shared, reworded.\nAlso shared, reworded.\n")
        self.assertEqual(back, "Shared, reworded.\n{{kit.only claude}}\nClaude only.\n{{kit.end}}\nAlso shared, reworded.\n")

    def test_changing_the_line_count_around_a_hidden_block_is_refused(self):
        source = "## Tools\nUse the shell.\n{{kit.only claude}}\nClaude only.\n{{kit.end}}\n## Reporting\nReport findings.\n"
        rendered = render.render_text(source, values("codex")).text
        edited = rendered.replace("Use the shell.\n## Reporting\n", "Use the shell carefully.\nPrefer read-only commands.\n## Output\n")
        with self.assertRaises(render.RenderError):
            render.carry_back_text(source, rendered, edited, values("codex"))

    def test_typed_block_markers_are_refused(self):
        source = "A.\nB.\n"
        for typed in ("{{kit.only codex}}\nSecret.\n{{kit.end}}\n", "<!-- agent-kit-sync only codex -->\n"):
            with self.subTest(typed=typed), self.assertRaises(render.RenderError):
                render.carry_back_text(source, source, source + typed, values("claude"))

    def test_comment_line_keeps_the_edits_line_break(self):
        source = "A\n{{kit.only claude}}\nB\n{{kit.end}}"
        back = carry(source, "claude", lambda t: t + "\nC\n")
        self.assertEqual(back, "A\n{{kit.only claude}}\nB\n{{kit.end}}\nC\n")
        crlf = "A\r\n{{kit.only claude}}\r\nB\r\n{{kit.end}}\r\nC\r\n"
        back = carry(crlf, "claude", lambda t: t.replace("B\r\n", "B2\r\n"))
        self.assertEqual(back, crlf.replace("B\r\n", "B2\r\n"))
        back = carry("A\n{{kit.only claude}}\nB\n{{kit.end}}\nC\n", "claude", lambda t: t.replace("\n", "\r\n"))
        self.assertEqual(back, "A\r\n{{kit.only claude}}\r\nB\r\n{{kit.end}}\r\nC\r\n")

    def test_skill_header_cannot_hold_a_block(self):
        source = "---\nname: s\n{{kit.only claude}}\nallowed-tools: Read\n{{kit.end}}\n---\nBody.\n"
        with self.assertRaises(render.RenderError):
            render.render_text(source, values("claude"), header=True)
        self.assertTrue(Checker(own_values=[]).placeholder_findings("m/skills/s/SKILL.md", source, header=True))

    def test_insertion_next_to_a_hidden_block_is_refused(self):
        source = "## Tools\nUse the shell.\n{{kit.only claude}}\nClaude only.\n{{kit.end}}\n## Reporting\n"
        rendered = render.render_text(source, values("codex")).text
        edited = rendered.replace("## Reporting\n", "## Notes\nKeep notes short.\n## Reporting\n")
        with self.assertRaises(render.RenderError):
            render.carry_back_text(source, rendered, edited, values("codex"))
        back = carry(source, "codex", lambda t: "Intro.\n" + t)
        self.assertEqual(back, "Intro.\n" + source)

    def test_comment_line_without_a_line_break_before_hidden_lines_is_refused(self):
        source = "A\n{{kit.only claude}}\nB\n{{kit.end}}\n{{kit.only codex}}\nC\n{{kit.end}}\n"
        rendered = render.render_text(source, values("claude")).text
        with self.assertRaisesRegex(render.RenderError, "add the line break"):
            render.carry_back_text(source, rendered, rendered.rstrip("\n"), values("claude"))

    def test_section_with_a_horizontal_rule_can_hold_a_block(self):
        text = "---\nIntro rule.\n{{kit.only claude}}\nClaude rule.\n{{kit.end}}\n---\nMore.\n"
        checker = Checker(own_values=[])
        self.assertEqual(checker.placeholder_findings("modules/w/sections/plain.md", text), [])
        self.assertTrue(checker.placeholder_findings("modules/w/agents/a.md", text, header=True))

    def test_installed_hook_command_is_machine_specific(self):
        checker = Checker(own_values=[])
        line = "Check with `/usr/bin/python3 -I -S {{kit.home}}/.agent-kit-sync/launcher.py run-hook mine/guard` first.\n"
        findings = checker.text_findings("a.md", line, own=True)
        self.assertTrue(any(f.check == "machine-specific" for f in findings))

    def test_markdown_template_with_an_unclosed_rule_renders(self):
        source = "---\nSee {{kit.module}}/notes.md for details.\n"
        self.assertEqual(render.render_text(source, values("claude"), header=True).text, "---\nSee /h/m/notes.md for details.\n")

    def test_documentation_of_run_hook_is_not_machine_specific(self):
        checker = Checker(own_values=[])
        line = "To try a hook by hand, run `python3 launcher.py run-hook MODULE/HOOK` in the kit folder.\n"
        self.assertFalse([f for f in checker.text_findings("a.md", line, own=True) if f.check == "machine-specific"])

    def test_codex_errors_count_lines_from_the_top_of_the_file(self):
        source = "---\nname: a\ndescription: d\n---\nBody.\n{{kit.end}}\n"
        with self.assertRaisesRegex(render.RenderError, "line 6:"):
            render.render_agent_codex(source, values("codex"))

    def test_plain_header_edit_with_hidden_fields_matches_earlier_behavior(self):
        source = "---\nname: a\ndescription: Helps\nclaude.tools: Read\ncodex.sandbox_mode: read-only\ncursor.model: fast\n---\nBody.\n"
        rendered = render.render_agent_md(source, "cursor", values("cursor")).text
        edited = rendered.replace("description: Helps\nmodel: fast\n", "description: >\n  Helps with reviews.\n")
        back = render.carry_back_agent_md(source, "cursor", rendered, edited, values("cursor"))
        self.assertEqual(render.render_agent_md(back, "cursor", values("cursor")).text, edited)

    def test_moving_a_line_across_a_comment_line_is_refused(self):
        source = "Read the task.\nFollow the plan.\n{{kit.only claude}}\nA hook sandboxes every command.\n{{kit.end}}\nReport back.\n"
        rendered = render.render_text(source, values("claude")).text
        moved = rendered.replace("A hook sandboxes every command.\n", "").replace("Read the task.\n", "Read the task.\nA hook sandboxes every command.\n")
        with self.assertRaises(render.RenderError):
            render.carry_back_text(source, rendered, moved, values("claude"))

    def test_changing_a_comment_line_is_refused(self):
        source = "A.\n{{kit.only claude}}\nB.\n{{kit.end}}\nC.\n"
        rendered = render.render_text(source, values("claude")).text
        for edited in (rendered.replace(END, ""), rendered.replace(START, "<!-- agent-kit-sync only codex -->\n"), rendered + START):
            with self.subTest(edited=edited), self.assertRaises(render.RenderError):
                render.carry_back_text(source, rendered, edited, values("claude"))

    def test_deleting_lines_keeps_each_on_its_side(self):
        source = "- one\n{{kit.only claude}}\n- two\n{{kit.end}}\n- two\n- three\n"
        back = carry(source, "claude", lambda t: t.replace(END + "- two\n- three\n", END))
        self.assertEqual(render.render_text(back, values("codex")).text, "- one\n")
        back = carry(source, "claude", lambda t: t.replace(START + "- two\n", START))
        self.assertEqual(render.render_text(back, values("codex")).text, "- one\n- two\n- three\n")

    def test_missing_last_line_break_before_hidden_lines_is_refused(self):
        source = "Shared.\n{{kit.only codex}}\nCodex note.\n{{kit.end}}\n"
        rendered = render.render_text(source, values("claude")).text
        with self.assertRaises(render.RenderError):
            render.carry_back_text(source, rendered, "Shared, edited.", values("claude"))
        plain = "One.\nTwo.\n"
        self.assertEqual(render.carry_back_text(plain, plain, "One.\nTwo, edited.", values("claude")), "One.\nTwo, edited.")
        form_feed = "Page one.\x0cPage two.\nLast.\n"
        self.assertEqual(render.carry_back_text(form_feed, form_feed, form_feed.replace("Last.", "Last, edited."), values("claude")),
                         form_feed.replace("Last.", "Last, edited."))

    def test_malformed_blocks_are_refused(self):
        for source in (
            "{{kit.only claude}}\nNo end.\n",
            "{{kit.end}}\n",
            "{{kit.only claude}}\n{{kit.only codex}}\n{{kit.end}}\n",
            "{{kit.only vscode}}\nx\n{{kit.end}}\n",
            "Inline {{kit.only claude}} marker.\n",
            "---\nname: a\n{{kit.only claude}}\ntools: Read\n{{kit.end}}\n---\nBody.\n",
            "Text\n<!-- agent-kit-sync end only -->\n",
        ):
            with self.subTest(source=source), self.assertRaises(render.RenderError):
                render.render_agent_md(source, "claude", values("claude"))

    def test_format_check_accepts_block_markers(self):
        checker = Checker(own_values=[])
        self.assertEqual(checker.placeholder_findings("a.md", "{{kit.only claude}}\nx\n{{kit.end}}\n"), [])

    def test_format_check_refuses_malformed_blocks(self):
        checker = Checker(own_values=[])
        for path, text in (
            ("a.md", "{{kit.end now}}\n"),
            ("a.md", "{{kit.only claude}}\nNo end.\n"),
            ("a.md", "{{kit.end}}\n"),
            ("a.md", "{{kit.only vscode}}\nx\n{{kit.end}}\n"),
            ("a.md", "{{kit.only claude,cursor}}\nx\n{{kit.end}}\n"),
            ("a.md", "Inline {{kit.only claude}} marker.\n"),
            ("a.md", "<!-- agent-kit-sync only claude -->\n"),
            ("a.json", "{{kit.only claude}}\nx\n{{kit.end}}\n"),
            ("m/agents/a.md", "---\nname: a\n{{kit.only claude}}\ntools: Read\n{{kit.end}}\n---\nBody.\n"),
        ):
            with self.subTest(text=text):
                self.assertTrue(checker.placeholder_findings(path, text, header="/agents/" in path))

    def test_random_edits_never_move_lines_between_apps(self):
        import random
        rng = random.Random(7)
        source = "Top.\n\n{{kit.only claude}}\nClaude note.\n\n{{kit.end}}\n\nMiddle.\n{{kit.only codex}}\nCodex note.\n{{kit.end}}\n- item\nEnd.\n"
        pool = ["Shared.\n", "\n", "- item\n", "Claude note.\n", "Codex note.\n", START, END]
        for app, other_block in (
            ("claude", "{{kit.only codex}}\nCodex note.\n{{kit.end}}\n"),
            ("codex", "{{kit.only claude}}\nClaude note.\n\n{{kit.end}}\n"),
        ):
            carried = 0
            for _ in range(3000):
                lines = render.render_text(source, values(app)).text.splitlines(keepends=True)
                for _step in range(rng.randint(1, 3)):
                    action = rng.choice(("insert", "delete", "replace"))
                    at = rng.randrange(len(lines) + 1)
                    if action == "insert":
                        lines.insert(at, rng.choice(pool))
                    elif at < len(lines):
                        if action == "delete":
                            del lines[at]
                        else:
                            lines[at] = rng.choice(pool)
                rendered = render.render_text(source, values(app)).text
                edited = "".join(lines)
                try:
                    back = render.carry_back_text(source, rendered, edited, values(app))
                except render.RenderError:
                    continue
                carried += 1
                # The copy renders back exactly, and its comment lines come only from the source's
                # markers, so each line is inside this app's block exactly when the user put it there.
                self.assertEqual(render.render_text(back, values(app)).text, edited)
                self.assertEqual(back.count("{{kit.only " + app + "}}\n"), 1, back)
                # The other app's block is never visible here, so no edit may change it.
                self.assertEqual(back.count(other_block), 1, back)
            self.assertGreater(carried, 500, app)

    def test_hook_command_is_reversed_only_as_a_whole_command(self):
        rendered = "Run `run guard` here, run guard-v2 there, and finally run guard.\n"
        source = render.carry_back_text("Old.\n", "Old.\n", rendered, values("claude"))
        self.assertEqual(source, "Run `{{kit.hook m/guard}}` here, run guard-v2 there, and finally {{kit.hook m/guard}}.\n")


if __name__ == "__main__":
    unittest.main()
