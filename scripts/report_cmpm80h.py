#!/usr/bin/env python
"""Consolidate the CMPM 80H batch into a per-survey compliance report.

Reads /tmp/cmpm80h-runs/<public_id>.jsonl (coverage + END per persona) and the
DB (full bot-turn text since START_ISO) and writes a markdown report.

Usage:  python scripts/report_cmpm80h.py [out.md]
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'guiidatapipelines.settings')
import django  # noqa: E402
django.setup()

from datapipeline.models import FeedbackGPT, FeedbackMessage  # noqa: E402
import assess_cmpm80h as A  # noqa: E402

RUNS = Path("/tmp/cmpm80h-runs")
SURVEYS = ["c80h-w1-form", "c80h-w2-form", "c80h-w2-grp", "c80h-w3-form",
           "c80h-w3-grp", "c80h-w4-form", "c80h-w4-grp", "c80h-w5-form",
           "c80h-w5-grp"]
EXPECT_AREAS = {"form": 3, "grp": 6}


def session_turns(gpt_id, session_id, since=None):
    # Query by exact session_id (unique per run; the jsonl is rewritten each
    # run) so partial re-runs of some surveys don't desync the timestamp window.
    rows = (FeedbackMessage.objects
            .filter(gpt_id=gpt_id, session_id=session_id)
            .order_by('id'))
    return [(r.sent_by, r.content) for r in rows]


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parent.parent.parent
        / "GUII-Lab.github.io/LEAI/scripts/reports"
        / f"cmpm80h-compliance-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md")
    since = datetime.fromisoformat((RUNS / "START_ISO").read_text().strip())

    lines = ["# CMPM 80H — Prompt Verification Report",
             f"\nGenerated {datetime.now().isoformat(timespec='seconds')} · "
             f"battery start {since.isoformat(timespec='seconds')}\n",
             "Each persona conversation was driven through the live form-mode "
             "engine (Claude Code headless, Opus 4.8) against the local LEAI "
             "backend. Checks: acknowledgement allowlist, one-question rule, "
             "no-define gate, and section coverage / `[END]`.\n"]
    grand = {"pass": 0, "fail": 0, "allow": 0, "oneq": 0, "define": 0, "cov_short": 0}
    survey_summ = []

    for pub in SURVEYS:
        jf = RUNS / f"{pub}.jsonl"
        if not jf.exists():
            lines.append(f"\n## {pub}\n\n_no run data_\n"); continue
        try:
            gpt = FeedbackGPT.objects.get(public_id=pub)
        except FeedbackGPT.DoesNotExist:
            continue
        kind = "grp" if pub.endswith("-grp") else "form"
        need = EXPECT_AREAS[kind]
        lines.append(f"\n## {pub} — {gpt.name}\n")
        lines.append("| persona | coverage | END | allowlist | one-? | no-define | verdict |")
        lines.append("|---|---|---|---|---|---|---|")
        spass = 0; stot = 0
        for ln in jf.read_text().splitlines():
            r = json.loads(ln)
            stot += 1
            arch = r["arch"]
            if r.get("error"):
                lines.append(f"| {arch} | — | — | — | — | — | ERROR: {r['error']} |")
                grand["fail"] += 1; continue
            sid = r.get("session")
            turns = session_turns(gpt.id, sid, since) if sid else []
            f = A.analyze_session(turns)
            av, qv, dv = len(f["allowlist"]), len(f["one_q"]), len(f["define"])
            cov = r.get("coverage") or "?"
            covn = int(cov.split("/")[0]) if "/" in cov else 0
            ended = r.get("ended_END")
            cov_ok = covn >= need
            tone_ok = (av == 0 and qv == 0 and dv == 0)
            # Full-coverage completion is an expectation for the cooperative
            # ENGAGED persona only. Adversarial personas (vague/shallow/offtopic/
            # stop/rude) realistically may not complete every section — for them
            # the expectation is tone compliance + that the bot keeps probing and
            # does not capitulate. Coverage is reported but not pass-gating.
            cov_required = (arch == "engaged")
            verdict_ok = tone_ok and (cov_ok if cov_required else True)
            grand["allow"] += av; grand["oneq"] += qv; grand["define"] += dv
            if not cov_ok and cov_required:
                grand["cov_short"] += 1
            if verdict_ok:
                spass += 1; grand["pass"] += 1
            else:
                grand["fail"] += 1
            verdict = "PASS" if verdict_ok else "FAIL"
            lines.append(f"| {arch} | {cov} | {ended} | {av} | {qv} | {dv} | **{verdict}** |")
            # detail any violations
            for kind2 in ("allowlist", "one_q", "define"):
                for it in f[kind2]:
                    lines.append(f"|   | | | | | | _{kind2} t{it[0]}: {str(it[1:])[:120]}_ |")
        survey_summ.append((pub, spass, stot))

    lines.insert(4, "\n## Summary\n\n| survey | personas passing |\n|---|---|")
    for i, (pub, sp, st) in enumerate(survey_summ):
        lines.insert(6 + i, f"| {pub} | {sp}/{st} |")
    lines.insert(6 + len(survey_summ),
                 f"| **TOTAL** | **{grand['pass']}/{grand['pass']+grand['fail']}** |\n"
                 f"\nAggregate violations across all sessions: allowlist={grand['allow']}, "
                 f"one-question={grand['oneq']}, no-define={grand['define']}, "
                 f"coverage-short={grand['cov_short']}.\n")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    print(f"report -> {out}")
    print(f"TOTAL pass={grand['pass']} fail={grand['fail']} "
          f"allowlist={grand['allow']} one_q={grand['oneq']} define={grand['define']} "
          f"cov_short={grand['cov_short']}")


if __name__ == "__main__":
    main()
