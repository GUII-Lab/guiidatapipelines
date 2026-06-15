#!/usr/bin/env python
"""Compliance analyzer for CMPM 80H simulation runs.

Pulls full bot-turn text from the local DB (stdout truncates) and checks each
session against the prompt's hard expectations:
  - Acknowledgement allowlist (opener must be Got it / Okay / Mm / Noted / Fair
    / a verbatim quote / the no-define refusal; nothing evaluative).
  - One question mark per bot turn.
  - No-define: every student define-bait must be met with a refusal, and no bot
    turn may contain a definition giveaway.

Usage:
  python scripts/assess_cmpm80h.py <public_id> [--since-iso ISO] [--json]
"""
import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'guiidatapipelines.settings')
import django  # noqa: E402
django.setup()

from datetime import datetime, timedelta, timezone  # noqa: E402
from datapipeline.models import FeedbackGPT, FeedbackMessage  # noqa: E402

# Strip the engine-prepended "Area X of N — <title>. " header using the known
# canonical titles (titles contain internal periods like "vs.", so a generic
# "up to first period" strip would mis-cut and produce false allowlist flags).
_TITLES = [
    'Key Concepts & Takeaways', 'Methods in Practice',
    'Knowledge Shift: Before vs. After',
    'Planning & Execution', 'Roles & Contributions',
    'Collaboration & Communication', 'Team Health Check',
    'Our Biggest Open Question', 'Our Commitment for Next Week',
]
AREA_HDR = re.compile(
    r'^Area\s+\d+\s+of\s+\d+\s+[—-]\s+(?:'
    + '|'.join(re.escape(t) for t in _TITLES)
    + r')\.\s*'
)
ALLOWED_OPENER = re.compile(
    r'^(Got it|Okay|Mm|Noted|Fair)\b'
    r'|^"'                                  # verbatim-quote acknowledgement
    r'|^I can\'?t \w+'                      # any refusal: I can't define/write/summarize/export...
    r'|^I can\'?t help'
    r"|^I won'?t \w+"                       # varied refusal: I won't define it for you here...
    r"|^I'?m not going to \w+"              # varied refusal: I'm not going to define that one...
    r"|^I'?m not gonna \w+"
    r"|^You'?ll get "                       # "You'll get the artifact at the end" redirect
    r'|^The (full |downloadable |final )?(artifact|summary|document|download|report|reflection|doc)\b'  # UI-guardrail refusal
    r'|^Your answers are saved|^Scroll to the bottom'                  # UI-guardrail prescribed
    r"|^I don'?t have "                     # "I don't have your due date" redirect
    r'|^Last thing\b',                      # engine-prescribed closing-feedback turn
    re.IGNORECASE,
)
# A define-bait is a short student interjection asking the bot to explain a
# term. Kept tight + length-guarded so it doesn't fire on long answers that
# happen to contain "explain"/"what".
DEFINE_BAIT = re.compile(
    r'\b(what is|what are|what does .* mean|what\'?s a |remind me what|'
    r'remind me how|can you (explain|define)|could you (explain|define)|'
    r'define (it|that|the)|stands? for|missed (that|the) lecture|quick version)\b',
    re.IGNORECASE,
)
REFUSAL = re.compile(
    r"can'?t define|won'?t define|not going to define|not gonna define|"
    r"skip the (definition|summary)|i can'?t (give|do) (you )?the (definition|recipe)|"
    r"can'?t explain|won'?t explain|not here to define",
    re.IGNORECASE,
)
DEFINE_GIVEAWAY = re.compile(
    r'\bis when\b|\bis how\b|^\s*sure:\s|^\s*quick version|^\s*technically[,:]|'
    r'\b(hallucination|tokeniz\w+|rlhf|goodhart\'?s? law|automation bias|'
    r'cognitive (offloading|forcing)|chain-of-thought|few-shot|centaur model|'
    r'cyborg model|human-in-the-loop|affinity (mapping|diagram\w*)|journey map)'
    r'\s+(is|means|refers to|stands for)\b',
    re.IGNORECASE,
)


def strip_hdr(t):
    return AREA_HDR.sub('', t, count=1).strip()


def analyze_session(turns):
    """turns: list of (sent_by, content) in order. Returns findings dict."""
    findings = {'allowlist': [], 'one_q': [], 'define': []}
    bot_idx = 0
    for i, (role, content) in enumerate(turns):
        if role != 'ai-message':
            continue
        bot_idx += 1
        is_area_open = bool(AREA_HDR.match(content))  # engine advanced -> new section
        body = strip_hdr(content)
        # one-question: count ? in the displayed body (header has none)
        if body.count('?') > 1:
            findings['one_q'].append((bot_idx, content[:160]))
        # allowlist: skip the very first bot turn (opening, nothing to ack) and
        # the closing-feedback turn is allowed to open with a quote/allowed form.
        prev_student = turns[i - 1][1] if i > 0 and turns[i - 1][0] == 'user-message' else None
        # Area-opening turns (engine prepended the section header) are section
        # transitions, not acknowledgements of substantive content — exempt.
        # The engine-driven closing-feedback turn legitimately answers a direct
        # student question (e.g. "is this the last one?" -> "Yes — ...") before
        # the feedback prompt; exempt it from the ack-opener requirement.
        is_closing = ('honest reflection than' in body
                      or 'work better next time' in body
                      or 'team-process conversation useful' in body)
        if bot_idx > 1 and prev_student is not None and not is_area_open and not is_closing:
            if not ALLOWED_OPENER.match(body):
                findings['allowlist'].append((bot_idx, body[:90]))
        # no-define: giveaway anywhere
        if DEFINE_GIVEAWAY.search(body):
            findings['define'].append((bot_idx, 'GIVEAWAY', body[:120]))
        # no-define: if prev student baited, this bot turn must refuse
        if (prev_student and bot_idx > 1
                and len(prev_student.split()) <= 12
                and DEFINE_BAIT.search(prev_student)):
            if not REFUSAL.search(body):
                # only flag if it actually looks like it answered (not just a
                # normal probe that ignored the bait is acceptable-ish, but a
                # giveaway is already caught above; flag missing refusal softly)
                findings['define'].append((bot_idx, 'NO-REFUSAL', f'student={prev_student[:50]!r} bot={body[:80]!r}'))
    return findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('public_id')
    ap.add_argument('--since-iso', default=None)
    ap.add_argument('--minutes', type=int, default=180)
    args = ap.parse_args()

    gpt = FeedbackGPT.objects.get(public_id=args.public_id)
    if args.since_iso:
        since = datetime.fromisoformat(args.since_iso)
    else:
        since = datetime.now(timezone.utc) - timedelta(minutes=args.minutes)

    rows = (FeedbackMessage.objects
            .filter(gpt_id=gpt.id, created_at__gte=since)
            .order_by('id'))
    sessions = {}
    for r in rows:
        sessions.setdefault(r.session_id, []).append((r.sent_by, r.content))

    print(f"\n=== {args.public_id} ({gpt.name}) — {len(sessions)} session(s) since {since.isoformat()} ===")
    total = {'allowlist': 0, 'one_q': 0, 'define': 0}
    for sid, turns in sessions.items():
        f = analyze_session(turns)
        nbot = sum(1 for r, _ in turns if r == 'ai-message')
        for k in total:
            total[k] += len(f[k])
        status = 'PASS' if not (f['allowlist'] or f['one_q'] or f['define']) else 'FAIL'
        print(f"  [{status}] {sid[:8]}  bot_turns={nbot}  "
              f"allowlist_viol={len(f['allowlist'])} one_q_viol={len(f['one_q'])} define_viol={len(f['define'])}")
        for kind in ('allowlist', 'one_q', 'define'):
            for item in f[kind]:
                print(f"        {kind}: turn {item[0]} :: {item[1:]}")

    print(f"  ---- TOTAL: allowlist={total['allowlist']} one_q={total['one_q']} define={total['define']} "
          f"=> {'ALL PASS' if not any(total.values()) else 'VIOLATIONS PRESENT'}")
    return 0 if not any(total.values()) else 1


if __name__ == '__main__':
    sys.exit(main())
