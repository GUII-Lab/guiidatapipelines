from __future__ import annotations

import secrets
import unicodedata
from io import BytesIO
from typing import Any

from django.db import IntegrityError, transaction

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas

from datapipeline.models import FeedbackGPT, FeedbackMessage, SurveyCompletionCertificate

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_GROUP_SIZE = 4
CODE_GROUP_COUNT = 4
CODE_SYMBOL_COUNT = CODE_GROUP_SIZE * CODE_GROUP_COUNT
VALID_MODES = {"general", "group", "form"}
MAX_CERTIFICATE_CREATE_ATTEMPTS = 32
CANVAS_GUIDANCE = "Submit as instructed by your instructor."


def normalize_code(value: str) -> str:
    compact = "".join(ch for ch in str(value).upper() if ch not in {" ", "-"})
    if len(compact) != CODE_SYMBOL_COUNT:
        raise ValueError("Completion certificate codes must have 16 symbols.")
    if any(ch not in CODE_ALPHABET for ch in compact):
        raise ValueError("Completion certificate code contains invalid characters.")
    return "-".join(
        compact[idx:idx + CODE_GROUP_SIZE]
        for idx in range(0, CODE_SYMBOL_COUNT, CODE_GROUP_SIZE)
    )


def generate_code() -> str:
    return "-".join(
        "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_GROUP_SIZE))
        for _ in range(CODE_GROUP_COUNT)
    )


def _clamp_int(value: Any, *, minimum: int | None = None, maximum: int | None = None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    number = value
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def _bounded_text(value: Any, *, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:maximum]


def _certificate_text(value: Any, *, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    text = "".join(
        " " if unicodedata.category(ch).startswith("C") else ch
        for ch in value
    )
    text = " ".join(text.split())
    return text or fallback


def build_display_snapshot(survey: FeedbackGPT) -> dict:
    week_value = survey.survey_label or (
        f"Week {survey.week_number}" if survey.week_number is not None else ""
    )
    snapshot = {
        "course_name": _certificate_text(
            survey.course.course_name if survey.course else None,
            fallback="Course unavailable",
        )[:200],
        "survey_name": _certificate_text(survey.name, fallback="Survey unavailable")[:100],
        "week_label": _certificate_text(week_value, fallback="Week unavailable")[:200],
        "canvas_guidance": CANVAS_GUIDANCE,
    }
    return snapshot


def _persist_display_snapshot_if_missing(
    certificate: SurveyCompletionCertificate,
    survey: FeedbackGPT,
) -> SurveyCompletionCertificate:
    if isinstance(certificate.display_snapshot, dict) and certificate.display_snapshot:
        return certificate

    display_snapshot = build_display_snapshot(survey)
    with transaction.atomic():
        locked = SurveyCompletionCertificate.objects.select_for_update().get(
            pk=certificate.pk
        )
        if isinstance(locked.display_snapshot, dict) and locked.display_snapshot:
            return locked
        locked.display_snapshot = display_snapshot
        locked.save(update_fields=["display_snapshot"])
        return locked


def _resolved_display_snapshot(certificate: SurveyCompletionCertificate) -> dict:
    fallback = build_display_snapshot(certificate.survey)
    raw_snapshot = (
        certificate.display_snapshot
        if isinstance(certificate.display_snapshot, dict)
        else None
    )
    raw = raw_snapshot or {}
    resolved = {
        "course_name": _certificate_text(
            raw.get("course_name"),
            fallback=fallback["course_name"],
        )[:200],
        "survey_name": _certificate_text(
            raw.get("survey_name"),
            fallback=fallback["survey_name"],
        )[:100],
        "week_label": _certificate_text(
            raw.get("week_label"),
            fallback=fallback["week_label"],
        )[:200],
    }
    # Submission guidance is product-wide copy rather than course metadata.
    # Always use the current wording, including when rendering certificates
    # issued before this wording changed.
    resolved["canvas_guidance"] = CANVAS_GUIDANCE
    return resolved


def sanitize_progress_snapshot(raw: object, *, mode: str, student_message_count: int) -> dict:
    payload = raw if isinstance(raw, dict) else {}
    clean_mode = mode if isinstance(mode, str) and mode in VALID_MODES else "general"
    clean_count = _clamp_int(student_message_count, minimum=0)
    complete = payload.get("complete")
    clean = {
        "version": 1,
        "mode": clean_mode,
        "student_message_count": clean_count or 0,
        "complete": complete if isinstance(complete, bool) else False,
    }

    if clean_mode != "form":
        return clean

    schema_id = _bounded_text(payload.get("schema_id"), maximum=100)
    if schema_id:
        clean["schema_id"] = schema_id

    area_index = _clamp_int(payload.get("area_index"), minimum=1, maximum=500)
    if area_index is not None:
        clean["area_index"] = area_index

    area_id = _bounded_text(payload.get("area_id"), maximum=100)
    if area_id:
        clean["area_id"] = area_id

    area_title = _bounded_text(payload.get("area_title"), maximum=200)
    if area_title:
        clean["area_title"] = area_title

    engine_turn = _clamp_int(payload.get("engine_turn"), minimum=0, maximum=10000)
    if engine_turn is not None:
        clean["engine_turn"] = engine_turn

    last_directive_kind = _bounded_text(payload.get("last_directive_kind"), maximum=100)
    if last_directive_kind:
        clean["last_directive_kind"] = last_directive_kind

    return clean


def eligible_student_message_count(survey_id: int, session_id: str) -> int:
    return (
        FeedbackMessage.objects
        .filter(
            gpt_id=survey_id,
            session_id=session_id,
            sent_by__in=["user", "user-message", "student"],
        )
        .exclude(source=FeedbackMessage.SOURCE_PDF)
        .count()
    )


def issue_or_get_certificate(
    survey: FeedbackGPT,
    session_id: str,
    raw_progress: object,
) -> SurveyCompletionCertificate:
    existing = SurveyCompletionCertificate.objects.filter(
        survey=survey,
        session_id=session_id,
    ).first()
    if existing:
        return _persist_display_snapshot_if_missing(existing, survey)

    snapshot = sanitize_progress_snapshot(
        raw_progress,
        mode=survey.mode,
        student_message_count=eligible_student_message_count(survey.pk, session_id),
    )
    display_snapshot = build_display_snapshot(survey)

    for _ in range(MAX_CERTIFICATE_CREATE_ATTEMPTS):
        code = generate_code()
        try:
            with transaction.atomic():
                certificate, _created = SurveyCompletionCertificate.objects.get_or_create(
                    survey=survey,
                    session_id=session_id,
                    defaults={
                        "code": normalize_code(code),
                        "progress_snapshot": snapshot,
                        "display_snapshot": display_snapshot,
                    },
                )
            return certificate
        except IntegrityError:
            existing = SurveyCompletionCertificate.objects.filter(
                survey=survey,
                session_id=session_id,
            ).first()
            if existing:
                return existing
            if SurveyCompletionCertificate.objects.filter(code=normalize_code(code)).exists():
                continue
            raise

    raise RuntimeError("Unable to allocate a unique completion certificate code.")


def _draw_wrapped_pdf_text(
    pdf: canvas.Canvas,
    text: str,
    *,
    x: float,
    y: float,
    width: float,
    font_name: str,
    font_size: float,
    leading: float,
    color: colors.Color,
) -> float:
    pdf.setFont(font_name, font_size)
    pdf.setFillColor(color)
    for line in simpleSplit(text, font_name, font_size, width):
        pdf.drawString(x, y, line)
        y -= leading
    return y


def render_certificate_pdf(certificate: SurveyCompletionCertificate) -> bytes:
    display = _resolved_display_snapshot(certificate)

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter, invariant=1)
    pdf.setTitle("GUII Lab Completion Certificate")
    pdf.setAuthor("GUII Lab Learning Experience AI")
    pdf.setSubject("Anonymous survey participation certificate")

    page_width, page_height = letter
    ink = colors.HexColor("#143447")
    accent = colors.HexColor("#B9863B")
    muted = colors.HexColor("#53646B")
    paper = colors.HexColor("#F8F7F2")
    panel = colors.HexColor("#EAF0F2")

    pdf.setFillColor(paper)
    pdf.rect(0, 0, page_width, page_height, fill=1, stroke=0)
    pdf.setStrokeColor(ink)
    pdf.setLineWidth(2.2)
    pdf.rect(30, 30, page_width - 60, page_height - 60, fill=0, stroke=1)
    pdf.setStrokeColor(accent)
    pdf.setLineWidth(0.7)
    pdf.rect(38, 38, page_width - 76, page_height - 76, fill=0, stroke=1)

    pdf.setFillColor(ink)
    pdf.setFont("Helvetica-Bold", 8.5)
    pdf.drawString(58, 735, "GUII LAB  /  LEARNING EXPERIENCE AI")
    pdf.setFont("Helvetica", 7.5)
    pdf.setFillColor(muted)
    pdf.drawRightString(554, 735, "ANONYMOUS PARTICIPATION RECORD")
    pdf.setStrokeColor(accent)
    pdf.setLineWidth(0.8)
    pdf.line(58, 720, 554, 720)

    pdf.setFillColor(ink)
    pdf.setStrokeColor(ink)
    pdf.setLineWidth(1.2)
    pdf.circle(526, 675, 21, fill=0, stroke=1)
    pdf.circle(526, 675, 15, fill=0, stroke=1)
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawCentredString(526, 672, "LEAI")
    pdf.setFont("Helvetica", 5.8)
    pdf.drawCentredString(526, 663, "CODED")

    pdf.setFont("Times-Bold", 26)
    pdf.drawCentredString(page_width / 2, 665, "GUII Lab Completion Certificate")
    pdf.setFillColor(accent)
    pdf.setFont("Helvetica-Bold", 8.5)
    pdf.drawCentredString(page_width / 2, 640, "A UNIQUE CODED RECORD FOR INSTRUCTOR VERIFICATION")
    pdf.setStrokeColor(accent)
    pdf.setLineWidth(1.2)
    pdf.line(200, 627, 412, 627)

    _draw_wrapped_pdf_text(
        pdf,
        "This certificate confirms that LEAI issued a unique code for the survey below after at least one response was saved. It does not reveal the student’s identity or survey responses.",
        x=90,
        y=595,
        width=432,
        font_name="Helvetica",
        font_size=9.5,
        leading=13,
        color=muted,
    )

    pdf.setFillColor(panel)
    pdf.roundRect(72, 412, 468, 125, 5, fill=1, stroke=0)
    field_x = 94
    value_x = 180
    fields = (
        ("COURSE", display["course_name"], 510),
        ("SURVEY", display["survey_name"], 474),
        ("WEEK", display["week_label"], 438),
    )
    for label, value, y in fields:
        pdf.setFillColor(ink)
        pdf.setFont("Helvetica-Bold", 7.5)
        pdf.drawString(field_x, y, label)
        _draw_wrapped_pdf_text(
            pdf,
            value,
            x=value_x,
            y=y - 1,
            width=330,
            font_name="Helvetica",
            font_size=10.5,
            leading=12,
            color=ink,
        )

    pdf.setFillColor(ink)
    pdf.roundRect(72, 304, 468, 79, 5, fill=1, stroke=0)
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 7.5)
    pdf.drawString(94, 360, "CERTIFICATE VERIFICATION CODE")
    pdf.setFont("Courier-Bold", 19)
    pdf.drawString(94, 327, certificate.code)
    pdf.setFillColor(colors.HexColor("#D7E3E7"))
    pdf.setFont("Helvetica", 7.5)
    pdf.drawRightString(518, 360, "KEEP THIS CODE WITH YOUR SUBMISSION")

    pdf.setFillColor(ink)
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(90, 263, "Verification required")
    _draw_wrapped_pdf_text(
        pdf,
        "Instructors must check this code in LEAI for the named survey. A visual copy alone is not proof of issuance.",
        x=90,
        y=247,
        width=432,
        font_name="Helvetica",
        font_size=9.5,
        leading=13,
        color=muted,
    )

    issued_on = certificate.issued_at.strftime("%B %-d, %Y") if certificate.issued_at else ""
    pdf.setStrokeColor(colors.HexColor("#C6D0D3"))
    pdf.setLineWidth(0.6)
    pdf.line(72, 148, 540, 148)
    pdf.setFillColor(ink)
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(72, 126, display["canvas_guidance"])
    pdf.setFillColor(muted)
    pdf.setFont("Helvetica", 7.5)
    pdf.drawString(72, 108, "Issued by Learning Experience AI (LEAI)  /  GUII Lab")
    if issued_on:
        pdf.drawRightString(540, 108, f"Code issued {issued_on}")

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()
