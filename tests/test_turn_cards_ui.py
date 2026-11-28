"""The Activity > Queue / Turns cards and the removal of Reason Trail and the Grillo tab."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from core.webui import SynthWebUIInterface

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(SynthWebUIInterface(autostart=False).app)


class TestTemplates:
    def test_activity_has_a_turns_tab_and_no_grillo_tab(
        self, client: TestClient
    ) -> None:
        html = client.get("/templates/history.html").text
        assert 'data-subtab="turns"' in html and 'id="subtab-turns"' in html
        assert 'id="history-turns-status"' in html
        assert "grillo" not in html.lower()

    def test_the_queue_has_a_place_for_the_running_cards(
        self, client: TestClient
    ) -> None:
        html = client.get("/templates/history.html").text
        assert 'id="history-queue-live"' in html
        assert html.index("history-queue-live") < html.index(
            'id="history-queue-content"'
        )

    def test_logs_no_longer_has_a_reason_trail(self, client: TestClient) -> None:
        html = client.get("/templates/logs.html").text
        assert "reason-trail" not in html and "Reason Trail" not in html
        assert 'data-logs-subtab="stats"' in html  # the others are untouched

    def test_the_cards_script_is_loaded_before_the_history_script(
        self, client: TestClient
    ) -> None:
        shell = (
            ROOT / "core" / "webui_templates" / "synth_webui_shell.html"
        ).read_text()
        assert shell.index("/js/turn_cards.js") < shell.index("/js/history.js")

    def test_no_script_still_asks_for_the_removed_endpoints(self) -> None:
        for name in ("history.js", "main.js"):
            text = (ROOT / "res" / "synth_webui" / "js" / name).read_text()
            assert "/api/reason-trail" not in text
            assert "/api/history/grillo" not in text


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
class TestQueueView:
    def test_the_queue_panel_scrolls_as_a_whole(self, client: TestClient) -> None:
        # "Running now" sits above the list; an expanded card made the panel taller than its
        # window and nothing scrolled it, because only the list below had a scrollbar.
        html = client.get("/templates/history.html").text
        assert "#subtab-queue { overflow-y: auto; }" in html
        assert (
            "#subtab-queue .history-container { flex: none; overflow: visible;" in html
        )

    def test_the_item_being_processed_is_not_listed_twice(self) -> None:
        # It is the live card under "Running now"; the list holds what waits.
        script = (
            "const fs=require('fs');const src=fs.readFileSync(process.argv[1],'utf8');"
            "const grab=(n)=>{const i=src.indexOf('function '+n);let d=0,j=src.indexOf('{',i);"
            "for(;j<src.length;j++){if(src[j]=='{')d++;if(src[j]=='}'){d--;if(!d)break;}}"
            "return src.slice(i,j+1)};"
            "const ctx={renderQueueItem:(it)=>'['+it.text_preview+']',formatTimestamp:(x)=>x};"
            "require('vm').runInNewContext(['_queueWaiting','_queueBuildHtml'].map(grab).join('\\n')"
            "+'\\nthis.build=_queueBuildHtml;',ctx);"
            "const run=(items)=>ctx.build({success:true,items});"
            "console.log(JSON.stringify({"
            "both:run([{status:'processing',text_preview:'now'},{status:'pending',text_preview:'next'}]),"
            "only:run([{status:'processing',text_preview:'now'}])}));"
        )
        done = subprocess.run(
            [
                "node",
                "-e",
                script,
                str(ROOT / "res" / "synth_webui" / "js" / "history.js"),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert done.returncode == 0, done.stderr
        drawn = json.loads(done.stdout)
        assert "[next]" in drawn["both"] and "[now]" not in drawn["both"]
        assert "1 item(s) queued" in drawn["both"]
        assert "Queue is empty" in drawn["only"] and "[now]" not in drawn["only"]


class TestCardsScript:
    @pytest.fixture(scope="class")
    def drawn(self) -> dict:
        done = subprocess.run(
            ["node", str(ROOT / "tests" / "js" / "turn_cards_check.js")],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout)

    def test_the_status_is_a_coloured_label(self, drawn: dict) -> None:
        assert (drawn["pillText"], drawn["rootClass"]) == (
            "Done",
            "tc-card tc-processed",
        )
        assert (drawn["failedPill"], drawn["failedClass"]) == (
            "Error",
            "tc-card tc-failed",
        )
        assert drawn["liveClass"] == "tc-card tc-processing"

    def test_a_card_is_collapsed_until_clicked_and_stays_open_after_a_redraw(
        self, drawn: dict
    ) -> None:
        assert drawn["collapsedAtFirst"] is True and drawn["openAfterClick"] is True
        assert drawn["openClass"] is True and drawn["stillOpen"] is True

    def test_the_sections_are_distinct_and_in_order(self, drawn: dict) -> None:
        assert drawn["sectionTitles"] == [
            "Summary",
            "Trigger",
            "Body (LLM)",
            "Recalled memories",
            "Reply",
        ]

    def test_json_is_formatted_with_its_keys(self, drawn: dict) -> None:
        assert '"engine"' in drawn["jsonKeys"] and '"recalled"' in drawn["jsonKeys"]

    def test_a_wrong_answer_is_marked_as_the_last_one_that_arrived(
        self, drawn: dict
    ) -> None:
        assert drawn["replyNote"] == "The correct answer."
        assert "last one that arrived" in drawn["failedNote"]
        assert drawn["failedError"] == "the engine timed out"

    def test_what_a_turn_contains_is_text_never_markup(self, drawn: dict) -> None:
        assert drawn["evilPreview"] == "<img src=x onerror=alert(1)>"
        assert drawn["evilTags"] == 0

    def test_the_lists_ask_the_right_endpoints(self, drawn: dict) -> None:
        assert drawn["requested"] == [
            "/api/turns/live",
            "/api/turns?limit=5&status=failed&search=x+y",
        ]
        assert drawn["liveTitle"] == "Running now" and drawn["liveHidden"] is False
        assert drawn["finishedCount"] == 2

    def test_a_redraw_keeps_a_block_scrolled_where_it_was(self, drawn: dict) -> None:
        # The running cards are drawn again every few seconds: without this, whatever
        # you scrolled inside an open card snapped back to the top.
        assert drawn["scrollKept"] == 120

    def test_empty_and_failed_loads_say_so(self, drawn: dict) -> None:
        assert drawn["emptyText"].startswith("No turns yet")
        assert drawn["brokenText"] == "Failed to load the turns."


class TestNoAnswerNote:
    def test_the_card_says_why_no_answer_arrived(self) -> None:
        script = (ROOT / "res" / "synth_webui" / "js" / "turn_cards.js").read_text(
            encoding="utf-8"
        )
        assert "function noAnswerNote(card)" in script
        assert "card.failures" in script and "card.error" in script
        assert "the model call failed" in script
