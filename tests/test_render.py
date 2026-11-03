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


class OnlyBlockTests(unittest.TestCase):
    def test_section_lines_appear_only_in_their_app(self):
        source = "Shared.\n{{kit.only codex}}\nCodex only.\n{{kit.end}}\nAlso shared.\n"
        self.assertEqual(render.render_text(source, values("codex")).text, "Shared.\nCodex only.\nAlso shared.\n")
        self.assertEqual(render.render_text(source, values("claude")).text, "Shared.\nAlso shared.\n")

    def test_block_can_list_several_apps(self):
        source = "{{kit.only claude cursor}}\nNot Codex.\n{{kit.end}}\n"
        self.assertEqual(render.render_text(source, values("cursor")).text, "Not Codex.\n")
        self.assertEqual(render.render_text(source, values("codex")).text, "")

    def test_agent_body_block_reaches_markdown_and_not_codex(self):
        claude = render.render_agent_md(AGENT, "claude", values("claude")).text
        self.assertIn("A hook runs `run guard` for this app only.", claude)
        self.assertNotIn("kit.only", claude)
        self.assertNotIn("kit.end", claude)
        codex = toml.loads(render.render_agent_codex(AGENT, values("codex")))
        self.assertEqual(codex["developer_instructions"], "First line.\n\nLast line.\n")
        self.assertEqual(codex["sandbox_mode"], "read-only")

    def test_edit_outside_a_block_keeps_the_block(self):
        rendered = render.render_agent_md(AGENT, "claude", values("claude")).text
        edited = rendered.replace("Last line.", "Final line.")
        back = render.carry_back_agent_md(AGENT, "claude", rendered, edited, values("claude"))
        self.assertEqual(back, AGENT.replace("Last line.", "Final line."))

    def test_edit_inside_a_block_stays_inside_it(self):
        rendered = render.render_agent_md(AGENT, "claude", values("claude")).text
        edited = rendered.replace("for this app only.", "for Claude Code only.")
        back = render.carry_back_agent_md(AGENT, "claude", rendered, edited, values("claude"))
        self.assertEqual(back, AGENT.replace("for this app only.", "for Claude Code only."))

    def test_codex_edit_keeps_the_other_apps_block(self):
        rendered = render.render_agent_codex(AGENT, values("codex"))
        edited = rendered.replace("Last line.", "Final line.")
        back = render.carry_back_agent_codex(AGENT, rendered, edited, values("codex"))
        self.assertEqual(back, AGENT.replace("Last line.", "Final line."))

    def test_lines_added_at_a_block_edge_are_refused(self):
        source = "Shared.\n{{kit.only claude}}\nClaude only.\n{{kit.end}}\nAlso shared.\n"
        rendered = render.render_text(source, values("codex")).text
        edited = rendered.replace("Shared.\n", "Shared.\nNew line.\n", 1)
        with self.assertRaises(render.RenderError):
            render.carry_back_text(source, rendered, edited, values("codex"))

    def test_lines_added_away_from_a_block_are_carried_back(self):
        source = "Top.\nShared.\n{{kit.only claude}}\nClaude only.\n{{kit.end}}\nAlso shared.\n"
        rendered = render.render_text(source, values("codex")).text
        edited = rendered.replace("Top.\n", "Top.\nNew line.\n", 1)
        back = render.carry_back_text(source, rendered, edited, values("codex"))
        self.assertEqual(back, source.replace("Top.\n", "Top.\nNew line.\n", 1))

    def test_malformed_blocks_are_refused(self):
        for source in (
            "{{kit.only claude}}\nNo end.\n",
            "{{kit.end}}\n",
            "{{kit.only claude}}\n{{kit.only codex}}\n{{kit.end}}\n",
            "{{kit.only vscode}}\nx\n{{kit.end}}\n",
            "Inline {{kit.only claude}} marker.\n",
            "---\nname: a\n{{kit.only claude}}\ntools: Read\n{{kit.end}}\n---\nBody.\n",
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
            ("m/agents/a.md", "---\nname: a\n{{kit.only claude}}\ntools: Read\n{{kit.end}}\n---\nBody.\n"),
        ):
            with self.subTest(text=text):
                self.assertTrue(checker.placeholder_findings(path, text))

    def assert_round_trip_or_refused(self, source, app, edited):
        """Either the edit is refused, or the carried-back source renders to the edit for this app."""
        rendered = render.render_text(source, values(app)).text
        try:
            back = render.carry_back_text(source, rendered, edited, values(app))
        except render.RenderError:
            return None
        self.assertEqual(render.render_text(back, values(app)).text, edited)
        return back

    def test_insert_beside_a_blank_line_at_a_block_edge_is_refused(self):
        source = "Intro.\n\n{{kit.only claude cursor}}\nA hook runs in a sandbox.\n{{kit.end}}\n\nOutro.\n"
        rendered = render.render_text(source, values("claude")).text
        edited = rendered.replace("sandbox.\n\n", "sandbox.\n\nIt blocks writes.\n\n")
        with self.assertRaises(render.RenderError):
            render.carry_back_text(source, rendered, edited, values("claude"))
        source = "Intro.\n\n{{kit.only codex}}\nCodex paragraph.\n\n{{kit.end}}\nOutro.\n"
        rendered = render.render_text(source, values("codex")).text
        edited = rendered.replace("paragraph.\n\n", "paragraph.\n\nAnother.\n\n")
        with self.assertRaises(render.RenderError):
            render.carry_back_text(source, rendered, edited, values("codex"))

    def test_replacement_that_adds_a_line_at_a_block_edge_is_refused(self):
        source = "First line.\n{{kit.only claude}}\nClaude note.\n{{kit.end}}\nLast line.\n"
        rendered = render.render_text(source, values("claude")).text
        edited = rendered.replace("Last line.\n", "Claude: use Read only.\nFinal line.\n")
        with self.assertRaises(render.RenderError):
            render.carry_back_text(source, rendered, edited, values("claude"))

    def test_rewording_lines_on_both_sides_of_a_hidden_block_keeps_it_in_place(self):
        source = "Shared.\n{{kit.only claude}}\nClaude only.\n{{kit.end}}\nAlso shared.\n"
        rendered = render.render_text(source, values("codex")).text
        edited = "Shared, reworded.\nAlso shared, reworded.\n"
        back = render.carry_back_text(source, rendered, edited, values("codex"))
        self.assertEqual(back, "Shared, reworded.\n{{kit.only claude}}\nClaude only.\n{{kit.end}}\nAlso shared, reworded.\n")

    def test_missing_last_line_break_before_hidden_lines_is_refused(self):
        source = "Shared.\n{{kit.only claude}}\nClaude note.\n{{kit.end}}\n"
        rendered = render.render_text(source, values("claude")).text
        with self.assertRaises(render.RenderError):
            render.carry_back_text(source, rendered, "Shared.\nClaude note, edited.", values("claude"))
        plain = "One.\nTwo.\n"
        self.assertEqual(render.carry_back_text(plain, plain, "One.\nTwo, edited.", values("claude")), "One.\nTwo, edited.")

    def test_deleting_a_line_that_matches_one_across_a_marker_is_refused(self):
        source = "A\n{{kit.only claude}}\nC\n\n{{kit.end}}\n\nE\n"
        rendered = render.render_text(source, values("claude")).text
        with self.assertRaises(render.RenderError):
            render.carry_back_text(source, rendered, rendered.replace("\n\n\n", "\n\n"), values("claude"))
        source = "- one\n{{kit.only claude}}\n- two\n{{kit.end}}\n- two\n- three\n"
        rendered = render.render_text(source, values("claude")).text
        with self.assertRaises(render.RenderError):
            render.carry_back_text(source, rendered, "- one\n- two\n- three\n", values("claude"))

    def test_deleting_a_whole_block_line_is_carried_back(self):
        source = "Shared.\n{{kit.only claude}}\nClaude one.\nClaude two.\n{{kit.end}}\nAlso shared.\n"
        rendered = render.render_text(source, values("claude")).text
        back = render.carry_back_text(source, rendered, rendered.replace("Claude one.\n", ""), values("claude"))
        self.assertEqual(back, source.replace("Claude one.\n", ""))

    def test_random_edits_keep_the_other_apps_block(self):
        import random
        rng = random.Random(7)
        pool = ["Shared.\n", "\n", "Other.\n", "- item\n", "Claude note.\n", "Codex note.\n"]
        source = "Top.\n\n{{kit.only claude}}\nClaude note.\n\n{{kit.end}}\n\nMiddle.\n{{kit.only codex}}\nCodex note.\n{{kit.end}}\n- item\nEnd.\n"
        codex_block = "{{kit.only codex}}\nCodex note.\n{{kit.end}}\n"
        carried = 0
        for _ in range(3000):
            lines = render.render_text(source, values("claude")).text.splitlines(keepends=True)
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
            back = self.assert_round_trip_or_refused(source, "claude", "".join(lines))
            if back is None:
                continue
            carried += 1
            # Claude Code cannot see Codex's block, so no edit made there may change it.
            self.assertEqual(back.count(codex_block), 1, back)
            render.render_text(back, values("codex"))
        self.assertGreater(carried, 1000)

    def test_hook_command_is_reversed_only_as_a_whole_command(self):
        rendered = "Run `run guard` here and run guard-v2 there.\n"
        source = render.carry_back_text("Old.\n", "Old.\n", rendered, values("claude"))
        self.assertEqual(source, "Run `{{kit.hook m/guard}}` here and run guard-v2 there.\n")


if __name__ == "__main__":
    unittest.main()
