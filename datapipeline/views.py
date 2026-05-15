from django.http import JsonResponse, HttpResponseBadRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from django.contrib.auth.hashers import make_password, check_password
from .models import *  # Ensure this is your custom User model
from . import openai_client
import json
import os
import secrets
import string
import csv
import io
from collections import defaultdict
from datetime import timedelta
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from django.conf import settings
from django.db import transaction
import requests


def _generate_public_id(length=12):
    """Generate a unique random alphanumeric public_id for FeedbackGPT."""
    alphabet = string.ascii_letters + string.digits
    for _ in range(10):  # retry up to 10 times on collision
        candidate = ''.join(secrets.choice(alphabet) for _ in range(length))
        if not FeedbackGPT.objects.filter(public_id=candidate).exists():
            return candidate
    raise RuntimeError('Could not generate a unique public_id after 10 attempts')



@csrf_exempt
def getOAI(request):
    return JsonResponse({'key':os.environ.get('oaiKey')}, safe=False, status=201)
    

@csrf_exempt
def message_create(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            session_id = data.get('session_id')
            student_id = data.get('student_id')
            sent_by = data.get('sent_by')
            content = data.get('content')
            gpt_used = data.get('gpt_used')

            message = Message(
                session_id=session_id,
                student_id=student_id,
                sent_by=sent_by,
                content=content,
                gpt_used=gpt_used,
            )
            message.save()

            return JsonResponse({'status': 'success', 'message': 'Feedback message saved successfully'})

        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)})


@csrf_exempt
def feedback_message_api(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            feedback_message = FeedbackMessage(
                session_id=data.get('session_id'),
                student_id=data.get('student_id'),
                sent_by=data.get('sent_by'),
                content=data.get('content'),
                gpt_used=data.get('gpt_used'),
                gpt_id=data.get('gpt_id'),
                research_consent=bool(data.get('research_consent', False)),
            )
            feedback_message.save()
            return JsonResponse({'status': 'success', 'message': 'Feedback message saved successfully'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)})


@csrf_exempt
def feedback_messages_bulk_api(request):
    """Atomically save a batch of feedback messages.

    Request body: {"messages": [ { session_id, student_id, sent_by, content,
    gpt_used, gpt_id, research_consent }, ... ]}
    Response: {"status": "success", "saved": N} or
             {"status": "error", "message": "...", "index": i}
    """
    if request.method != 'POST':
        return HttpResponse(status=405, content='Method not allowed')
    try:
        data = json.loads(request.body)
    except ValueError as e:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON: ' + str(e)}, status=400)

    messages = data.get('messages')
    if not isinstance(messages, list):
        return JsonResponse({'status': 'error', 'message': 'messages must be a list'}, status=400)
    if not messages:
        return JsonResponse({'status': 'error', 'message': 'messages list is empty'}, status=400)

    required = ('session_id', 'student_id', 'sent_by', 'content')
    objs = []
    for idx, m in enumerate(messages):
        if not isinstance(m, dict):
            return JsonResponse({'status': 'error', 'message': 'message must be an object', 'index': idx}, status=400)
        missing = [k for k in required if not m.get(k)]
        if missing:
            return JsonResponse({
                'status': 'error',
                'message': 'missing fields: ' + ', '.join(missing),
                'index': idx,
            }, status=400)
        objs.append(FeedbackMessage(
            session_id=m.get('session_id'),
            student_id=m.get('student_id'),
            sent_by=m.get('sent_by'),
            content=m.get('content'),
            gpt_used=m.get('gpt_used') or '',
            gpt_id=m.get('gpt_id'),
            research_consent=bool(m.get('research_consent', False)),
        ))

    try:
        with transaction.atomic():
            FeedbackMessage.objects.bulk_create(objs)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)

    return JsonResponse({'status': 'success', 'saved': len(objs)})


@csrf_exempt  # For simplicity, but handle CSRF properly in production
def create_new_gpt(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        try:
            new_gpt = CustomGPT(
                name=data['name'],
                created_by=data['created_by'],
                university=data['university'],
                gpt_type=data['gpt_type'],
                instructions=data['instructions']
            )
            new_gpt.save()
            return JsonResponse({"message": "Custom GPT created successfully", "id": new_gpt.id})
        except Exception as e:
            return HttpResponse(status=400, content="Error in creating Custom GPT: " + str(e))
    else:
        return HttpResponse(status=405, content="Method not allowed")


@csrf_exempt
def list_custom_gpts(request):
    if request.method == 'GET':
        gpts = CustomGPT.objects.all().order_by('-created_at').values('id', 'name', 'instructions')
        return JsonResponse(list(gpts), safe=False)
    else:
        return HttpResponse(status=405, content="Method not allowed")
    

@csrf_exempt
def list_feedback_gpts(request):
    if request.method == 'GET':
        gpts = FeedbackGPT.objects.all().values(
            'id', 'public_id', 'name', 'instructions', 'week_number', 'survey_label',
            'course__course_id', 'course__course_name'
        )
        return JsonResponse(list(gpts), safe=False)
    else:
        return HttpResponse(status=405, content="Method not allowed")


@csrf_exempt
def create_course(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            course_id = data.get('course_id', '').strip()
            if not course_id:
                return JsonResponse({'error': 'course_id is required'}, status=400)
            if Course.objects.filter(course_id=course_id).exists():
                return JsonResponse({'error': 'A course with this ID already exists'}, status=409)
            course = Course.objects.create(
                course_id=course_id,
                course_name=data.get('course_name', ''),
                instructor_name=data.get('instructor_name', ''),
                password=make_password(data.get('password', '')),
            )
            return JsonResponse({'status': 'success', 'id': course.id, 'course_id': course.course_id})
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return HttpResponse(status=405)


@csrf_exempt
def verify_course_password(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            course_id = data.get('course_id', '')
            password = data.get('password', '')
            try:
                course = Course.objects.get(course_id=course_id)
            except Course.DoesNotExist:
                return JsonResponse({'valid': False, 'error': 'Course not found'}, status=404)
            if check_password(password, course.password):
                return JsonResponse({
                    'valid': True,
                    'course_name': course.course_name,
                    'instructor_name': course.instructor_name,
                })
            return JsonResponse({'valid': False, 'error': 'Incorrect password'}, status=401)
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return HttpResponse(status=405)


@csrf_exempt
def create_feedback_gpt(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            course_id = data.get('course_id')
            course = None
            if course_id:
                try:
                    course = Course.objects.get(course_id=course_id)
                except Course.DoesNotExist:
                    return JsonResponse({'error': 'Course not found'}, status=404)

            # Default expiry: 14 days from now
            raw_expires = data.get('expires_at')
            if raw_expires:
                expires_at = parse_datetime(raw_expires)
            else:
                expires_at = timezone.now() + timedelta(days=14)

            raw_opens = data.get('opens_at')
            opens_at = parse_datetime(raw_opens) if raw_opens else None

            mode = data.get('mode', 'general')
            if mode not in ('general', 'group', 'form'):
                return JsonResponse({'error': "mode must be 'general', 'group', or 'form'"}, status=400)
            team_configuration_id = data.get('team_configuration_id')
            form_schema_id = (data.get('form_schema_id') or '').strip()
            form_schema = None
            # 'form' REQUIRES a schema; 'group' may OPTIONALLY bind a schema to
            # get engine-driven coverage on top of the team-picker flow.
            if mode == 'form' and not form_schema_id:
                return JsonResponse({
                    'error': "form_schema_id is required when mode='form'",
                }, status=400)
            if form_schema_id:
                if mode == 'general':
                    return JsonResponse({
                        'error': "form_schema_id is only valid for mode='form' or mode='group'",
                    }, status=400)
                try:
                    form_schema = FormSchema.objects.get(schema_id=form_schema_id, is_active=True)
                except FormSchema.DoesNotExist:
                    return JsonResponse({'error': 'form_schema not found or inactive'}, status=404)
            source_cfg = None
            if mode == 'group':
                if not team_configuration_id:
                    return JsonResponse({
                        'error': "team_configuration_id is required when mode='group'",
                    }, status=400)
                try:
                    source_cfg = TeamConfiguration.objects.get(id=team_configuration_id)
                except TeamConfiguration.DoesNotExist:
                    return JsonResponse({'error': 'team_configuration not found'}, status=404)
                if source_cfg.archived:
                    return JsonResponse({
                        'error': 'team_configuration is archived; unarchive before using',
                    }, status=400)
                if course and source_cfg.course_id != course.id:
                    return JsonResponse({
                        'error': 'team_configuration belongs to a different course',
                    }, status=400)

            with transaction.atomic():
                gpt = FeedbackGPT.objects.create(
                    name=data.get('name', ''),
                    instructions=data.get('instructions', ''),
                    created_by=data.get('instructor_name', ''),
                    course=course,
                    week_number=data.get('week_number'),
                    survey_label=data.get('survey_label', ''),
                    public_id=_generate_public_id(),
                    expires_at=expires_at,
                    opens_at=opens_at,
                    is_closed=False,
                    anonymity_mode=data.get('anonymity_mode', 'anonymous'),
                    reporting_structure=data.get('reporting_structure', ''),
                    canvas_integration=data.get('canvas_integration', False),
                    mode=mode,
                    form_schema=form_schema,
                )
                snapshot_payload = None
                if mode == 'group' and source_cfg is not None:
                    snap = SurveyTeamSnapshot.objects.create(
                        survey=gpt,
                        source_configuration=source_cfg,
                        name=source_cfg.name,
                        label_prefix=source_cfg.label_prefix,
                        color=source_cfg.color,
                    )
                    for t in source_cfg.teams.all():
                        SurveyTeam.objects.create(
                            snapshot=snap, number=t.number, size=t.size,
                            display_name=t.display_name,
                        )
                    snapshot_payload = _survey_snapshot_to_dict(snap)

            resp = {
                'status': 'success',
                'id': gpt.id,
                'public_id': gpt.public_id,
                'name': gpt.name,
                'mode': gpt.mode,
                'form_schema_id': form_schema.schema_id if form_schema else None,
                'expires_at': gpt.expires_at.isoformat() if gpt.expires_at else None,
            }
            if snapshot_payload:
                resp['team_snapshot'] = snapshot_payload
            return JsonResponse(resp)
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return HttpResponse(status=405)


@csrf_exempt
def feedback_gpts_by_course(request):
    if request.method == 'GET':
        course_id = request.GET.get('course_id')
        if not course_id:
            return JsonResponse({'error': 'course_id parameter is required'}, status=400)
        try:
            course = Course.objects.get(course_id=course_id)
        except Course.DoesNotExist:
            return JsonResponse({'error': 'Course not found'}, status=404)
        gpts = FeedbackGPT.objects.filter(course=course).order_by('week_number', 'created_at')
        result = []
        for gpt in gpts:
            session_ids = FeedbackMessage.objects.filter(gpt_id=gpt.id).values_list('session_id', flat=True).distinct()
            session_count = session_ids.count()
            msg_count = FeedbackMessage.objects.filter(gpt_id=gpt.id).count()
            avg_turns = round(msg_count / session_count) if session_count else 0
            snap = getattr(gpt, 'team_snapshot', None)
            result.append({
                'id': gpt.id,
                'public_id': gpt.public_id,
                'name': gpt.name,
                'mode': gpt.mode,
                'week_number': gpt.week_number,
                'survey_label': gpt.survey_label,
                'instructions': gpt.instructions,
                'created_at': gpt.created_at.isoformat(),
                'expires_at': gpt.expires_at.isoformat() if gpt.expires_at else None,
                'opens_at': gpt.opens_at.isoformat() if gpt.opens_at else None,
                'is_closed': gpt.is_closed,
                'anonymity_mode': gpt.anonymity_mode,
                'reporting_structure': gpt.reporting_structure,
                'session_count': session_count,
                'avg_turns': avg_turns,
                'team_configuration_id': snap.source_configuration_id if snap else None,
                'team_snapshot_name': snap.name if snap else None,
                'team_snapshot_color': snap.color if snap else None,
                'form_schema_id': gpt.form_schema.schema_id if gpt.form_schema_id else None,
            })
        return JsonResponse(result, safe=False)
    return HttpResponse(status=405)


@csrf_exempt
def get_feedback_gpt_by_public_id(request):
    if request.method == 'GET':
        public_id = request.GET.get('public_id')
        if not public_id:
            return JsonResponse({'error': 'public_id parameter is required'}, status=400)
        try:
            gpt = FeedbackGPT.objects.get(public_id=public_id)
        except FeedbackGPT.DoesNotExist:
            return JsonResponse({'error': 'Survey not found'}, status=404)

        # Server-side lifecycle check
        now = timezone.now()
        is_active = True
        reason = None
        if gpt.is_closed:
            is_active = False
            reason = 'closed'
        elif gpt.opens_at and gpt.opens_at > now:
            is_active = False
            reason = 'not_yet_open'
        elif gpt.expires_at and gpt.expires_at < now:
            is_active = False
            reason = 'expired'

        snap = getattr(gpt, 'team_snapshot', None)
        return JsonResponse({
            'id': gpt.id,
            'public_id': gpt.public_id,
            'name': gpt.name,
            'mode': gpt.mode,
            'instructions': gpt.instructions,
            'week_number': gpt.week_number,
            'survey_label': gpt.survey_label,
            'is_active': is_active,
            'reason': reason,
            'expires_at': gpt.expires_at.isoformat() if gpt.expires_at else None,
            'opens_at': gpt.opens_at.isoformat() if gpt.opens_at else None,
            'anonymity_mode': gpt.anonymity_mode,
            'canvas_integration': gpt.canvas_integration,
            'team_snapshot': _survey_snapshot_to_dict(snap) if snap else None,
            'form_schema_id': gpt.form_schema.schema_id if gpt.form_schema_id else None,
            # Inline the schema body so feedback.html doesn't need a second
            # round-trip to render Area-N-of-N transitions on first paint.
            'form_schema': (
                {
                    'schema_id': gpt.form_schema.schema_id,
                    'version': gpt.form_schema.version,
                    'title': gpt.form_schema.title,
                    'body': gpt.form_schema.body,
                }
                if gpt.form_schema_id else None
            ),
        })


@csrf_exempt
def list_form_schemas(request):
    """GET /form_schemas/?active=1 — registry for the PromptDesigner form tab."""
    if request.method != 'GET':
        return HttpResponse(status=405)
    qs = FormSchema.objects.all()
    if request.GET.get('active') in ('1', 'true', 'True'):
        qs = qs.filter(is_active=True)
    return JsonResponse([
        {
            'schema_id': s.schema_id,
            'version': s.version,
            'title': s.title,
            'course_label': s.course_label,
            'week_number': s.week_number,
            'is_active': s.is_active,
            'section_count': len((s.body or {}).get('sections', [])),
            'updated_at': s.updated_at.isoformat(),
        }
        for s in qs
    ], safe=False)


@csrf_exempt
def get_form_schema(request, schema_id):
    """GET /form_schemas/<schema_id>/ — full schema body for engine + insights."""
    if request.method != 'GET':
        return HttpResponse(status=405)
    try:
        s = FormSchema.objects.get(schema_id=schema_id)
    except FormSchema.DoesNotExist:
        return JsonResponse({'error': 'form_schema not found'}, status=404)
    return JsonResponse({
        'schema_id': s.schema_id,
        'version': s.version,
        'title': s.title,
        'course_label': s.course_label,
        'week_number': s.week_number,
        'is_active': s.is_active,
        'body': s.body,
        'updated_at': s.updated_at.isoformat(),
    })
    return HttpResponse(status=405)


@csrf_exempt
def feedback_messages_by_gpt(request):
    if request.method == 'GET':
        gpt_id = request.GET.get('gpt_id')
        if not gpt_id:
            return JsonResponse({'error': 'gpt_id parameter is required'}, status=400)
        messages = FeedbackMessage.objects.filter(gpt_id=gpt_id).order_by('created_at')
        sessions = defaultdict(list)
        for m in messages:
            sessions[m.session_id].append({
                'id': m.id,
                'session_id': m.session_id,
                'sent_by': m.sent_by,
                'content': m.content,
                'created_at': m.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                # New: tells the analyzer this row is from an instructor-
                # uploaded PDF (renders 📄 badge + drives Source filter).
                'source': m.source,
                'pdf_batch_id': str(m.pdf_batch_id) if m.pdf_batch_id else None,
            })
        return JsonResponse({
            'sessions': dict(sessions),
            'session_count': len(sessions),
            'message_count': messages.count(),
        })
    return HttpResponse(status=405)


@csrf_exempt
def feedback_messages_by_course(request):
    if request.method == 'GET':
        course_id = request.GET.get('course_id')
        if not course_id:
            return JsonResponse({'error': 'course_id parameter is required'}, status=400)
        try:
            course = Course.objects.get(course_id=course_id)
        except Course.DoesNotExist:
            return JsonResponse({'error': 'Course not found'}, status=404)
        gpts = FeedbackGPT.objects.filter(course=course).order_by('week_number', 'created_at')
        result = []
        for gpt in gpts:
            messages = FeedbackMessage.objects.filter(gpt_id=gpt.id).order_by('created_at')
            sessions = defaultdict(list)
            for m in messages:
                sessions[m.session_id].append({
                    'sent_by': m.sent_by,
                    'content': m.content,
                    'created_at': m.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                    'source': m.source,
                    'pdf_batch_id': str(m.pdf_batch_id) if m.pdf_batch_id else None,
                })
            session_count = len(sessions)
            msg_count = messages.count()
            avg_turns = round(msg_count / session_count) if session_count else 0
            result.append({
                'gpt_id': gpt.id,
                'name': gpt.name,
                'mode': gpt.mode,
                'week_number': gpt.week_number,
                'survey_label': gpt.survey_label,
                'sessions': dict(sessions),
                'session_count': session_count,
                'avg_turns': avg_turns,
                'is_closed': gpt.is_closed,
                'expires_at': gpt.expires_at.isoformat() if gpt.expires_at else None,
                'opens_at': gpt.opens_at.isoformat() if gpt.opens_at else None,
                'canvas_integration': gpt.canvas_integration,
                'form_schema_id': gpt.form_schema.schema_id if gpt.form_schema_id else None,
            })
        return JsonResponse(result, safe=False)
    return HttpResponse(status=405)
    

@csrf_exempt
def set_survey_status(request):
    """Close or reopen a survey. Reopening resets expires_at to now+14d."""
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            survey_id = data.get('survey_id')
            action = data.get('action')  # 'close' or 'reopen'
            if not survey_id or action not in ('close', 'reopen'):
                return JsonResponse({'error': 'survey_id and action (close|reopen) required'}, status=400)
            try:
                gpt = FeedbackGPT.objects.get(id=survey_id)
            except FeedbackGPT.DoesNotExist:
                return JsonResponse({'error': 'Survey not found'}, status=404)
            if action == 'close':
                gpt.is_closed = True
            else:
                gpt.is_closed = False
                gpt.expires_at = timezone.now() + timedelta(days=14)
            gpt.save()
            return JsonResponse({
                'status': 'success',
                'is_closed': gpt.is_closed,
                'expires_at': gpt.expires_at.isoformat() if gpt.expires_at else None,
            })
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return HttpResponse(status=405)


@csrf_exempt
def update_survey(request):
    """Update editable fields on an existing survey."""
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            survey_id = data.get('survey_id')
            if not survey_id:
                return JsonResponse({'error': 'survey_id required'}, status=400)
            try:
                gpt = FeedbackGPT.objects.get(id=survey_id)
            except FeedbackGPT.DoesNotExist:
                return JsonResponse({'error': 'Survey not found'}, status=404)

            updatable = ['name', 'survey_label', 'week_number', 'instructions',
                         'anonymity_mode', 'reporting_structure', 'canvas_integration']
            for field in updatable:
                if field in data:
                    setattr(gpt, field, data[field])

            if 'expires_at' in data:
                gpt.expires_at = parse_datetime(data['expires_at']) if data['expires_at'] else None
            if 'opens_at' in data:
                gpt.opens_at = parse_datetime(data['opens_at']) if data['opens_at'] else None

            # Swap team configuration for an existing group-mode survey. Only
            # allowed when no student has self-assigned to a team yet — once
            # assignments exist, switching would orphan their picks. In that
            # case instructors should duplicate the survey instead.
            new_cfg_id = data.get('team_configuration_id')
            snapshot_payload = None
            if new_cfg_id is not None and gpt.mode == 'group':
                try:
                    new_cfg_id = int(new_cfg_id)
                except (TypeError, ValueError):
                    return JsonResponse({'error': 'team_configuration_id must be an integer'}, status=400)
                current_snap = getattr(gpt, 'team_snapshot', None)
                current_source_id = current_snap.source_configuration_id if current_snap else None
                if new_cfg_id != current_source_id:
                    try:
                        new_cfg = TeamConfiguration.objects.get(id=new_cfg_id)
                    except TeamConfiguration.DoesNotExist:
                        return JsonResponse({'error': 'team_configuration not found'}, status=404)
                    if new_cfg.archived:
                        return JsonResponse({
                            'error': 'team_configuration is archived; unarchive before using',
                        }, status=400)
                    if gpt.course_id and new_cfg.course_id != gpt.course_id:
                        return JsonResponse({
                            'error': 'team_configuration belongs to a different course',
                        }, status=400)
                    if current_snap and SessionTeamAssignment.objects.filter(
                        survey_team__snapshot=current_snap
                    ).exists():
                        return JsonResponse({
                            'error': "Can't switch team configuration: students have already picked teams in this survey. Duplicate the survey instead so existing responses keep their team picks.",
                        }, status=400)
                    with transaction.atomic():
                        if current_snap:
                            current_snap.delete()
                        snap = SurveyTeamSnapshot.objects.create(
                            survey=gpt,
                            source_configuration=new_cfg,
                            name=new_cfg.name,
                            label_prefix=new_cfg.label_prefix,
                            color=new_cfg.color,
                        )
                        for t in new_cfg.teams.all():
                            SurveyTeam.objects.create(
                                snapshot=snap, number=t.number, size=t.size,
                                display_name=t.display_name,
                            )
                        snapshot_payload = _survey_snapshot_to_dict(snap)

            gpt.save()
            resp = {'status': 'success', 'id': gpt.id}
            if snapshot_payload is not None:
                resp['team_snapshot'] = snapshot_payload
            return JsonResponse(resp)
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return HttpResponse(status=405)


@csrf_exempt
def delete_survey(request):
    """Permanently delete a survey and all its student responses.

    Destructive — removes the FeedbackGPT row plus every FeedbackMessage
    whose gpt_id points at it (FeedbackMessage has no FK, so we clean up
    manually to avoid orphaned response rows).
    """
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            survey_id = data.get('survey_id')
            if not survey_id:
                return JsonResponse({'error': 'survey_id required'}, status=400)
            try:
                gpt = FeedbackGPT.objects.get(id=survey_id)
            except FeedbackGPT.DoesNotExist:
                return JsonResponse({'error': 'Survey not found'}, status=404)
            messages_deleted, _ = FeedbackMessage.objects.filter(gpt_id=gpt.id).delete()
            gpt.delete()
            return JsonResponse({
                'status': 'success',
                'survey_id': survey_id,
                'messages_deleted': messages_deleted,
            })
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return HttpResponse(status=405)


@csrf_exempt
def clone_survey(request):
    """Duplicate a survey, resetting lifecycle and generating a new public_id."""
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            survey_id = data.get('survey_id')
            if not survey_id:
                return JsonResponse({'error': 'survey_id required'}, status=400)
            try:
                src = FeedbackGPT.objects.get(id=survey_id)
            except FeedbackGPT.DoesNotExist:
                return JsonResponse({'error': 'Survey not found'}, status=404)

            with transaction.atomic():
                clone = FeedbackGPT.objects.create(
                    name='Copy of ' + src.name,
                    instructions=src.instructions,
                    created_by=src.created_by,
                    course=src.course,
                    week_number=src.week_number,
                    survey_label=src.survey_label,
                    public_id=_generate_public_id(),
                    expires_at=timezone.now() + timedelta(days=14),
                    opens_at=None,
                    is_closed=False,
                    anonymity_mode=src.anonymity_mode,
                    reporting_structure=src.reporting_structure,
                    mode=src.mode,
                )
                # For group-mode surveys, take a fresh snapshot from the same source
                # configuration (if still available) so the clone gets current team sizes.
                src_snap = getattr(src, 'team_snapshot', None)
                if src.mode == 'group' and src_snap and src_snap.source_configuration_id:
                    source_cfg = src_snap.source_configuration
                    new_snap = SurveyTeamSnapshot.objects.create(
                        survey=clone,
                        source_configuration=source_cfg,
                        name=source_cfg.name,
                        label_prefix=source_cfg.label_prefix,
                        color=source_cfg.color,
                    )
                    for t in source_cfg.teams.all():
                        SurveyTeam.objects.create(
                            snapshot=new_snap, number=t.number, size=t.size,
                            display_name=t.display_name,
                        )

            return JsonResponse({
                'status': 'success',
                'id': clone.id,
                'public_id': clone.public_id,
                'name': clone.name,
                'mode': clone.mode,
            })
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return HttpResponse(status=405)


def _session_to_code(session_id):
    """Mirror the client-side JS hash to produce a completion code from a session ID.

    JS original:
        var hash = 0;
        for (var i = 0; i < sid.length; i++) {
            hash = ((hash << 5) - hash) + sid.charCodeAt(i);
            hash |= 0;  // 32-bit signed int
        }
        Math.abs(hash).toString(36).toUpperCase().padStart(6,'0').slice(0,6)
    """
    h = 0
    for ch in session_id:
        h = ((h << 5) - h) + ord(ch)
        # Emulate JS `|= 0` — clamp to signed 32-bit
        h &= 0xFFFFFFFF
        if h >= 0x80000000:
            h -= 0x100000000
    n = abs(h)
    if n == 0:
        return '000000'
    digits = '0123456789abcdefghijklmnopqrstuvwxyz'
    result = ''
    while n:
        result = digits[n % 36] + result
        n //= 36
    return result.upper().zfill(6)[:6]


@csrf_exempt
def export_survey_responses(request):
    """Return CSV of all responses for a survey."""
    if request.method == 'GET':
        survey_id = request.GET.get('survey_id')
        if not survey_id:
            return JsonResponse({'error': 'survey_id required'}, status=400)
        try:
            gpt = FeedbackGPT.objects.get(id=survey_id)
        except FeedbackGPT.DoesNotExist:
            return JsonResponse({'error': 'Survey not found'}, status=404)

        messages = FeedbackMessage.objects.filter(gpt_id=gpt.id).order_by('session_id', 'created_at')
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(['completion_code', 'session_id', 'sent_by', 'content', 'created_at'])
        for m in messages:
            writer.writerow([_session_to_code(m.session_id), m.session_id, m.sent_by, m.content, m.created_at.strftime('%Y-%m-%d %H:%M:%S')])

        filename = f'survey_{gpt.id}_responses.csv'
        response = HttpResponse(buf.getvalue(), content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
    return HttpResponse(status=405)


@csrf_exempt
def sendFireData(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            fireDataInstance = FireData.objects.create(data=data)
            return JsonResponse(fireDataInstance.data, safe=False, status=201)
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)
    
    if request.method == 'GET':
        try:
            latest_data = FireData.objects.latest('id')  # or use 'created_at' if your model has a timestamp field
            return JsonResponse(latest_data.data, safe=False, status=200)
        except FireData.DoesNotExist:
            return JsonResponse({'error': 'No data found'}, status=404)



@csrf_exempt
def feedbackList(request):
    messages = FeedbackMessage.objects.all()
    grouped_messages = defaultdict(list)

    # Group messages by session_id
    for message in messages:
        grouped_messages[message.session_id].append({
            "id": message.id,
            "session_id": message.session_id,
            "student_id": message.student_id,
            "sent_by": message.sent_by,
            "created_at": message.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "content": message.content,
            "gpt_used": message.gpt_used,
        })

    # Convert defaultdict to dict for JSON serialization
    grouped_messages_dict = dict(grouped_messages)

    return JsonResponse(grouped_messages_dict, safe=False)  # safe=False is needed to allow non-dict objects


@csrf_exempt
def scList(request):
    messages = Message.objects.all()
    grouped_messages = defaultdict(list)

    # Group messages by session_id
    for message in messages:
        grouped_messages[message.session_id].append({
            "id": message.id,
            "session_id": message.session_id,
            "student_id": message.student_id,
            "sent_by": message.sent_by,
            "created_at": message.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "content": message.content,
            "gpt_used": message.gpt_used,
        })

    # Convert defaultdict to dict for JSON serialization
    grouped_messages_dict = dict(grouped_messages)

    return JsonResponse(grouped_messages_dict, safe=False)  # safe=False is needed to allow non-dict objects

@csrf_exempt
def get_messages_by_gpt(request):
    gpt_used = request.GET.get('gpt_used')

    if not gpt_used:
        return JsonResponse({'error': 'The gpt_used parameter is required.'}, status=400)

    messages = Message.objects.filter(gpt_used=gpt_used).values(
        'session_id', 'student_id', 'sent_by', 'created_at', 'content', 'gpt_used'
    )

    messages_list = list(messages)

    return JsonResponse(messages_list, safe=False)

@csrf_exempt
def get_lets_by_gpt(request):
    gpt_used = request.GET.get('gpt_used')

    if not gpt_used:
        return JsonResponse({'error': 'The gpt_used parameter is required.'}, status=400)

    messages = FeedbackMessage.objects.filter(gpt_used=gpt_used).values(
        'session_id', 'student_id', 'sent_by', 'created_at', 'content', 'gpt_used'
    )

    messages_list = list(messages)

    return JsonResponse(messages_list, safe=False)


@csrf_exempt
def upload_image(request):
    if request.method == 'POST':
        try:
            # Check if image file is present in the request
            if 'image' not in request.FILES:
                return JsonResponse({'error': 'No image file provided'}, status=400)
            
            image_file = request.FILES['image']
            
            # Validate file type (optional - you can add more validation)
            allowed_types = ['image/jpeg', 'image/png', 'image/gif', 'image/webp']
            if image_file.content_type not in allowed_types:
                return JsonResponse({'error': 'Invalid file type. Only JPEG, PNG, GIF, and WebP are allowed.'}, status=400)
            
            # Get optional fields from form data
            title = request.POST.get('title', '')
            description = request.POST.get('description', '')
            
            # Create Image instance
            image_instance = Image(
                image=image_file,
                title=title,
                description=description
            )
            image_instance.save()
            
            # Return success response with image URL
            return JsonResponse({
                'status': 'success',
                'message': 'Image uploaded successfully',
                'image_id': image_instance.id,
                'image_url': image_instance.image_url,
                'title': image_instance.title,
                'description': image_instance.description,
                'uploaded_at': image_instance.uploaded_at.isoformat()
            })
            
        except Exception as e:
            return JsonResponse({'error': f'Error uploading image: {str(e)}'}, status=500)
    
    else:
        return JsonResponse({'error': 'Only POST method allowed'}, status=405)


@csrf_exempt
def get_image(request, image_id):
    """Get image details by ID"""
    try:
        image_instance = Image.objects.get(id=image_id)
        return JsonResponse({
            'id': image_instance.id,
            'image_url': image_instance.image_url,
            'title': image_instance.title,
            'description': image_instance.description,
            'uploaded_at': image_instance.uploaded_at.isoformat()
        })
    except Image.DoesNotExist:
        return JsonResponse({'error': 'Image not found'}, status=404)


@csrf_exempt
def list_images(request):
    """List all uploaded images"""
    if request.method == 'GET':
        images = Image.objects.all().order_by('-uploaded_at')
        images_data = []
        for img in images:
            images_data.append({
                'id': img.id,
                'image_url': img.image_url,
                'title': img.title,
                'description': img.description,
                'uploaded_at': img.uploaded_at.isoformat()
            })
        return JsonResponse(images_data, safe=False)
    else:
        return JsonResponse({'error': 'Only GET method allowed'}, status=405)


@csrf_exempt
def openai_chat(request):
    """Proxy endpoint for the Responses API chat turn.

    Contract (unchanged from the legacy Chat Completions implementation):
        POST body: {chat_history: [...], user_text: str, model?: str}
        200 body:  {status: "success", response: str, usage: {...}}

    All SDK calls go through datapipeline.openai_client — this view is a
    thin adapter that parses the request, catches typed client errors, and
    maps them to JsonResponse status codes."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST method allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    user_text = data.get('user_text')
    if not user_text:
        return JsonResponse({'error': 'user_text is required'}, status=400)

    try:
        result = openai_client.run_chat(
            chat_history=data.get('chat_history', []),
            user_text=user_text,
            model=data.get('model'),
            temperature=data.get('temperature'),
        )
    except openai_client.OpenAIClientError as e:
        return JsonResponse({'error': e.detail}, status=e.status_code)

    return JsonResponse({
        'status': 'success',
        'response': result['response'],
        'usage': result['usage'],
    })


@csrf_exempt
def openai_structured(request):
    """Proxy endpoint for schema-enforced structured output via Responses API.

    POST body: {
        chat_history?: [...],
        user_text: str,
        json_schema: dict,       # full JSON Schema object
        schema_name?: str,       # optional; default "structured_response"
        model?: str              # optional; default OPENAI_DEFAULT_MODEL
    }
    200 body: {status, response (raw JSON str), parsed (dict|list), usage}
    422 body: {error, reason: "refusal_or_unparseable"}  on refusal/bad JSON
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST method allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    user_text = data.get('user_text')
    json_schema = data.get('json_schema')
    if not user_text:
        return JsonResponse({'error': 'user_text is required'}, status=400)
    if not isinstance(json_schema, dict):
        return JsonResponse(
            {'error': 'json_schema (object) is required'}, status=400,
        )

    try:
        result = openai_client.run_structured(
            chat_history=data.get('chat_history', []),
            user_text=user_text,
            json_schema=json_schema,
            schema_name=data.get('schema_name', 'structured_response'),
            model=data.get('model'),
            temperature=data.get('temperature'),
        )
    except openai_client.OpenAIRefusalError as e:
        return JsonResponse(
            {'error': e.detail, 'reason': 'refusal_or_unparseable'},
            status=422,
        )
    except openai_client.OpenAIClientError as e:
        return JsonResponse({'error': e.detail}, status=e.status_code)

    return JsonResponse({
        'status': 'success',
        'response': result['response'],
        'parsed': result['parsed'],
        'usage': result['usage'],
    })


_TTS_FORMAT_MIME = {
    'mp3': 'audio/mpeg',
    'opus': 'audio/ogg',
    'aac': 'audio/aac',
    'flac': 'audio/flac',
    'wav': 'audio/wav',
    'pcm': 'application/octet-stream',
}


@csrf_exempt
def openai_tts(request):
    """Proxy endpoint for OpenAI text-to-speech.

    POST body: {text: str, voice?: str, model?: str, format?: str}
    200 body:  raw audio bytes with appropriate Content-Type."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST method allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    text = (data.get('text') or '').strip()
    if not text:
        return JsonResponse({'error': 'text is required'}, status=400)

    fmt = (data.get('format') or 'mp3').lower()

    try:
        audio_bytes = openai_client.synthesize_speech(
            text=text,
            voice=data.get('voice'),
            model=data.get('model'),
            response_format=fmt,
        )
    except openai_client.OpenAIClientError as e:
        return JsonResponse({'error': e.detail}, status=e.status_code)

    return HttpResponse(
        audio_bytes,
        content_type=_TTS_FORMAT_MIME.get(fmt, 'application/octet-stream'),
    )


@csrf_exempt
def openai_stt(request):
    """Proxy endpoint for OpenAI speech-to-text (Whisper).

    POST multipart/form-data:
        file: audio blob (webm/mp4/wav/mp3/m4a/ogg/flac, <=25MB)
        language?: ISO-639-1 hint
        prompt?: context prompt to bias terminology
        model?: override default STT model

    200 body: {status: "success", text: str, model: str}"""
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST method allowed'}, status=405)

    upload = request.FILES.get('file')
    if upload is None:
        return JsonResponse({'error': 'file is required'}, status=400)

    try:
        result = openai_client.transcribe_audio(
            file_obj=upload,
            filename=upload.name or 'audio.webm',
            model=request.POST.get('model'),
            language=request.POST.get('language') or None,
            prompt=request.POST.get('prompt') or None,
            content_type=getattr(upload, 'content_type', None),
        )
    except openai_client.OpenAIClientError as e:
        return JsonResponse({'error': e.detail}, status=e.status_code)

    return JsonResponse({
        'status': 'success',
        'text': result['text'],
        'model': result['model'],
    })


# ---------------------------------------------------------------------------
# LEAI Chat Session helpers
# ---------------------------------------------------------------------------

def _chat_message_to_dict(msg):
    """Serialize a LEAIChatMessage to a plain dict for API responses."""
    return {
        'id': msg.pk,
        'role': msg.role,
        'text': msg.text,
        'cited': msg.cited,
        'status': msg.status,
        'error': msg.error,
        'created_at': msg.created_at.isoformat(),
    }


def _recover_stale_chat_messages(session):
    """Flip any of this session's pending/running messages past the stale
    threshold to failed, so the UI can retry instead of polling forever
    after a dyno cycle mid-turn. Mirrors the QuickTake recovery path."""
    from . import leai_analysis
    pending = session.messages.filter(
        status__in=[
            LEAIChatMessage.STATUS_PENDING, LEAIChatMessage.STATUS_RUNNING,
        ]
    )
    stale_pks = [m.pk for m in pending if leai_analysis._is_chat_message_stale(m)]
    if stale_pks:
        LEAIChatMessage.objects.filter(pk__in=stale_pks).update(
            status=LEAIChatMessage.STATUS_FAILED,
            error='Generation did not complete in time. Please retry.',
        )


def _session_detail_response(session, status=200):
    """Serialize a LEAIChatSession (with messages) to a JsonResponse.

    Includes a `corpus` array built with the session's own scope. This is
    the authoritative rid→response mapping the assistant used when
    generating turns, so the frontend can resolve citation popovers
    without rebuilding the index (and without risking a scope mismatch
    that would silently show the wrong response text).
    """
    _recover_stale_chat_messages(session)
    messages = list(
        session.messages.order_by('created_at').values(
            'id', 'role', 'text', 'cited', 'status', 'error', 'created_at'
        )
    )
    for m in messages:
        m['created_at'] = m['created_at'].isoformat()

    # Build the same corpus the chat turn used. Cheap (read-only query +
    # in-memory group/sort) and small enough to ship in the detail payload.
    from . import leai_analysis
    corpus = leai_analysis.build_response_corpus(
        course=session.course,
        scope_kind=session.scope_kind,
        scope_week_number=session.scope_week_number,
        scope_survey_ids=list(session.scope_survey_ids or []),
        scope_session_ids=list(session.scope_session_ids or []),
    )

    return JsonResponse({
        'id': str(session.pk),
        'course_id': session.course.course_id,
        'title': session.title,
        'scope': {
            'kind': session.scope_kind,
            'week_number': session.scope_week_number,
            'survey_ids': session.scope_survey_ids,
            'session_ids': session.scope_session_ids,
        },
        'system_prompt_override': session.system_prompt_override,
        'created_at': session.created_at.isoformat(),
        'updated_at': session.updated_at.isoformat(),
        'messages': messages,
        'corpus': corpus,
    }, status=status)


def _quicktake_to_dict(qt):
    """Serialize a LEAIQuickTake instance to a plain dict."""
    return {
        'id': qt.pk,
        'course_id': qt.course.course_id,
        'scope_key': qt.scope_key,
        'bullets': qt.bullets,
        'tensions': qt.tensions,
        'gaps': qt.gaps,
        'team_health': qt.team_health,
        'form_sections': qt.form_sections,
        'verification': qt.verification,
        'system_prompt': qt.system_prompt,
        'user_text': qt.user_text,
        'model_name': qt.model_name,
        'status': qt.status,
        'error': qt.error,
        'job_started_at': qt.job_started_at.isoformat() if qt.job_started_at else None,
        'created_at': qt.created_at.isoformat(),
        'updated_at': qt.updated_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# LEAI Chat Sessions — list + create
# ---------------------------------------------------------------------------

@csrf_exempt
def leai_chat_sessions_list(request):
    """GET /api/leai_chat_sessions/  — list sessions for a course.
    POST /api/leai_chat_sessions/    — create a new session.
    """
    if request.method == 'GET':
        course_id = request.GET.get('course_id')
        if not course_id:
            return JsonResponse({'error': 'course_id is required'}, status=400)
        try:
            course = Course.objects.get(course_id=course_id)
        except Course.DoesNotExist:
            return JsonResponse({'error': 'Course not found'}, status=404)

        sessions = LEAIChatSession.objects.filter(course=course).order_by('-updated_at')
        session_list = []
        for s in sessions:
            session_list.append({
                'id': str(s.pk),
                'title': s.title,
                'scope': {
                    'kind': s.scope_kind,
                    'week_number': s.scope_week_number,
                    'survey_ids': s.scope_survey_ids,
                    'session_ids': s.scope_session_ids,
                },
                'message_count': s.messages.count(),
                'created_at': s.created_at.isoformat(),
                'updated_at': s.updated_at.isoformat(),
            })
        return JsonResponse({'sessions': session_list})

    if request.method == 'POST':
        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({'error': 'Invalid JSON'}, status=400)

        course_id = data.get('course_id')
        if not course_id:
            return JsonResponse({'error': 'course_id is required'}, status=400)
        try:
            course = Course.objects.get(course_id=course_id)
        except Course.DoesNotExist:
            return JsonResponse({'error': 'Course not found'}, status=404)

        scope = data.get('scope', {})
        session = LEAIChatSession.objects.create(
            course=course,
            title=data.get('title', 'New chat'),
            scope_kind=scope.get('kind', 'course'),
            scope_week_number=scope.get('week_number'),
            scope_survey_ids=scope.get('survey_ids') or [],
            scope_session_ids=scope.get('session_ids') or [],
            system_prompt_override=data.get('system_prompt_override'),
        )

        seed = data.get('seed_system_message')
        if seed:
            LEAIChatMessage.objects.create(
                session=session,
                role='system',
                text=seed,
                cited=[],
            )

        # Optional: seed a visible assistant message (e.g. a Quick Take handoff)
        # with its own citation array. Must be a dict with `text` and
        # `cited` (list of {rid, pill_index, verdict?}).
        seed_assistant = data.get('seed_assistant_message')
        if seed_assistant and isinstance(seed_assistant, dict):
            text = seed_assistant.get('text') or ''
            cited = seed_assistant.get('cited') or []
            if not isinstance(cited, list):
                cited = []
            if text:
                LEAIChatMessage.objects.create(
                    session=session,
                    role='assistant',
                    text=text,
                    cited=cited,
                )

        return _session_detail_response(session, status=201)

    return JsonResponse({'error': 'Method not allowed'}, status=405)


# ---------------------------------------------------------------------------
# LEAI Chat Session — detail, patch, delete
# ---------------------------------------------------------------------------

@csrf_exempt
def leai_chat_session_detail(request, session_id):
    """GET/PATCH/DELETE /api/leai_chat_sessions/<uuid>/"""
    try:
        session = LEAIChatSession.objects.get(pk=session_id)
    except LEAIChatSession.DoesNotExist:
        return JsonResponse({'error': 'Session not found'}, status=404)

    if request.method == 'GET':
        return _session_detail_response(session)

    if request.method == 'PATCH':
        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({'error': 'Invalid JSON'}, status=400)

        if 'title' in data:
            session.title = data['title']
        if 'system_prompt_override' in data:
            session.system_prompt_override = data['system_prompt_override']
        if 'scope' in data:
            scope = data['scope']
            if 'kind' in scope:
                session.scope_kind = scope['kind']
            if 'week_number' in scope:
                session.scope_week_number = scope['week_number']
            if 'survey_ids' in scope:
                session.scope_survey_ids = scope['survey_ids']
            if 'session_ids' in scope:
                session.scope_session_ids = scope['session_ids']
        session.save()

        return JsonResponse({
            'id': str(session.pk),
            'course_id': session.course.course_id,
            'title': session.title,
            'scope': {
                'kind': session.scope_kind,
                'week_number': session.scope_week_number,
                'survey_ids': session.scope_survey_ids,
                'session_ids': session.scope_session_ids,
            },
            'system_prompt_override': session.system_prompt_override,
            'created_at': session.created_at.isoformat(),
            'updated_at': session.updated_at.isoformat(),
        })

    if request.method == 'DELETE':
        session.delete()
        return HttpResponse(status=204)

    return JsonResponse({'error': 'Method not allowed'}, status=405)


# ---------------------------------------------------------------------------
# LEAI Chat Session — turn
# ---------------------------------------------------------------------------

@csrf_exempt
def leai_chat_session_turn(request, session_id):
    """POST /api/leai_chat_sessions/<uuid>/turn/

    Enqueues a chat turn in a background thread and returns 202 with both
    the saved user message and the placeholder assistant message
    (status=pending). Clients poll
    ``GET /api/leai_chat_sessions/<uuid>/messages/<id>/`` on the assistant
    message until status flips to ready or failed.

    Async because corpus build + LLM call + verify_claims can exceed
    Heroku's 30s router timeout once the scope spans multiple surveys.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    try:
        session = LEAIChatSession.objects.get(pk=session_id)
    except LEAIChatSession.DoesNotExist:
        return JsonResponse({'error': 'Session not found'}, status=404)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    user_text = data.get('user_text', '').strip()
    if not user_text:
        return JsonResponse({'error': 'user_text is required'}, status=400)

    from . import leai_analysis
    try:
        user_msg, assistant_msg = leai_analysis.start_chat_turn_job(
            session=session, user_text=user_text,
        )
    except ValueError as e:
        return JsonResponse({'error': str(e)}, status=400)

    return JsonResponse({
        'user_message': _chat_message_to_dict(user_msg),
        'message': _chat_message_to_dict(assistant_msg),
        'session_updated_at': session.updated_at.isoformat(),
    }, status=202)


@csrf_exempt
def leai_chat_message_detail(request, session_id, message_id):
    """GET /api/leai_chat_sessions/<uuid>/messages/<int>/

    Returns a single chat message. Used by the frontend to poll a pending
    assistant message until its status is ready or failed. Mirrors the
    stale-recovery semantics of leai_quicktake_fetch_or_delete: a
    pending/running row past CHAT_TURN_JOB_STALE_SECONDS is reported as
    failed so the UI can retry.
    """
    if request.method != 'GET':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    try:
        msg = LEAIChatMessage.objects.get(pk=message_id, session_id=session_id)
    except LEAIChatMessage.DoesNotExist:
        return JsonResponse({'error': 'Message not found'}, status=404)

    from . import leai_analysis
    if leai_analysis._is_chat_message_stale(msg):
        LEAIChatMessage.objects.filter(pk=msg.pk).update(
            status=LEAIChatMessage.STATUS_FAILED,
            error='Generation did not complete in time. Please retry.',
        )
        msg.refresh_from_db()

    return JsonResponse(_chat_message_to_dict(msg))


# ---------------------------------------------------------------------------
# LEAI Quick Take — fetch or delete
# ---------------------------------------------------------------------------

@csrf_exempt
def leai_quicktake_fetch_or_delete(request):
    """GET/DELETE /api/leai_quicktake/?course_id=<id>&scope_key=<key>"""
    course_id = request.GET.get('course_id')
    scope_key = request.GET.get('scope_key')

    if not course_id or not scope_key:
        return JsonResponse({'error': 'course_id and scope_key are required'}, status=400)

    try:
        course = Course.objects.get(course_id=course_id)
    except Course.DoesNotExist:
        return JsonResponse({'error': 'Course not found'}, status=404)

    try:
        qt = LEAIQuickTake.objects.get(course=course, scope_key=scope_key)
    except LEAIQuickTake.DoesNotExist:
        return JsonResponse({'error': 'QuickTake not found'}, status=404)

    if request.method == 'GET':
        from . import leai_analysis
        # Recover from dyno-cycle zombies: a row stuck in pending/running
        # past the stale window is reported as failed so the UI can retry.
        if leai_analysis._is_job_stale(qt):
            LEAIQuickTake.objects.filter(pk=qt.pk).update(
                status=LEAIQuickTake.STATUS_FAILED,
                error='Generation did not complete in time. Please retry.',
            )
            qt.refresh_from_db()
        return JsonResponse(_quicktake_to_dict(qt))

    if request.method == 'DELETE':
        qt.delete()
        return HttpResponse(status=204)

    return JsonResponse({'error': 'Method not allowed'}, status=405)


# ---------------------------------------------------------------------------
# LEAI Quick Take — generate
# ---------------------------------------------------------------------------

@csrf_exempt
def leai_quicktake_generate(request):
    """POST /api/leai_quicktake/generate/

    Enqueues a quicktake job in a background thread and returns 202 with
    the current row (status=pending or running). Clients poll GET
    /api/leai_quicktake/ until status is ready or failed.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    course_id = data.get('course_id')
    scope_key = data.get('scope_key')
    scope = data.get('scope')

    if not course_id or not scope_key or not scope:
        return JsonResponse(
            {'error': 'course_id, scope_key, and scope are required'}, status=400
        )

    try:
        course = Course.objects.get(course_id=course_id)
    except Course.DoesNotExist:
        return JsonResponse({'error': 'Course not found'}, status=404)

    from . import leai_analysis
    try:
        qt, _started = leai_analysis.start_quicktake_job(
            course=course,
            scope_key=scope_key,
            scope_kind=scope.get('kind', 'course'),
            scope_week_number=scope.get('week_number'),
            scope_survey_ids=scope.get('survey_ids'),
            scope_session_ids=scope.get('session_ids'),
        )
    except ValueError as e:
        return JsonResponse({'error': str(e)}, status=400)

    return JsonResponse(_quicktake_to_dict(qt), status=202)



# ================= In-Group feedback endpoints =================
# Palette order must match CONFIG_COLOR_PALETTE in LEAI/leai-shared.js so
# auto-assigned colors stay consistent across frontend ↔ backend.
_CONFIG_COLOR_PALETTE = ['forest', 'plum', 'amber', 'teal', 'rose', 'indigo', 'brown', 'slate']


def _pick_next_config_color(existing_configs):
    """Return the first palette color not already used by a sibling configuration.
    Cycles by count when all 8 colors are in use."""
    used = {c.color for c in existing_configs}
    for color in _CONFIG_COLOR_PALETTE:
        if color not in used:
            return color
    return _CONFIG_COLOR_PALETTE[len(list(existing_configs)) % len(_CONFIG_COLOR_PALETTE)]


def _team_configuration_to_dict(cfg):
    return {
        'id': cfg.id,
        'courseId': cfg.course.course_id if cfg.course else None,
        'name': cfg.name,
        'label_prefix': cfg.label_prefix,
        'color': cfg.color,
        'archived': cfg.archived,
        'teams': [
            {'id': t.id, 'number': t.number, 'size': t.size,
             'display_name': t.display_name}
            for t in cfg.teams.all().order_by('number')
        ],
        'created_at': cfg.created_at.isoformat(),
        'updated_at': cfg.updated_at.isoformat(),
    }


def _survey_snapshot_to_dict(snap):
    return {
        'id': snap.id,
        'survey_id': snap.survey_id,
        'source_configuration_id': snap.source_configuration_id,
        'name': snap.name,
        'label_prefix': snap.label_prefix,
        'color': snap.color,
        'teams': [
            {'id': t.id, 'number': t.number, 'size': t.size,
             'display_name': t.display_name}
            for t in snap.teams.all().order_by('number')
        ],
        'created_at': snap.created_at.isoformat(),
    }


def _assignment_to_dict(a):
    return {
        'id': a.id,
        'session_id': a.session_id,
        'survey_team_id': a.survey_team_id,
        'assigned_at': a.assigned_at.isoformat(),
    }


@csrf_exempt
def list_team_configurations(request):
    """GET /team_configurations/?course_id=...&include_archived=0|1"""
    if request.method != 'GET':
        return HttpResponse(status=405)
    course_id = request.GET.get('course_id')
    if not course_id:
        return JsonResponse({'error': 'course_id required'}, status=400)
    try:
        course = Course.objects.get(course_id=course_id)
    except Course.DoesNotExist:
        return JsonResponse({'error': 'Course not found'}, status=404)
    include_archived = request.GET.get('include_archived') in ('1', 'true')
    qs = TeamConfiguration.objects.filter(course=course)
    if not include_archived:
        qs = qs.filter(archived=False)
    qs = qs.order_by('created_at')
    return JsonResponse([_team_configuration_to_dict(c) for c in qs], safe=False)


@csrf_exempt
def create_team_configuration(request):
    """POST /team_configurations/create/  body: {course_id, name, label_prefix, teams:[{number,size}...], color?}"""
    if request.method != 'POST':
        return HttpResponse(status=405)
    try:
        data = json.loads(request.body)
        course_id = data.get('course_id')
        name = (data.get('name') or 'Primary').strip()
        label_prefix = data.get('label_prefix') or 'Team'
        teams = data.get('teams') or []
        explicit_color = data.get('color')
        if not course_id:
            return JsonResponse({'error': 'course_id required'}, status=400)
        try:
            course = Course.objects.get(course_id=course_id)
        except Course.DoesNotExist:
            return JsonResponse({'error': 'Course not found'}, status=404)

        siblings = list(TeamConfiguration.objects.filter(course=course, archived=False))
        sibling_names = {c.name for c in siblings}
        # Auto-dedupe: "Primary" → "Primary 2" → "Primary 3"
        if name in sibling_names:
            n = 2
            while f'{name} {n}' in sibling_names:
                n += 1
            name = f'{name} {n}'

        color = explicit_color if explicit_color in dict(COLOR_CHOICES) else _pick_next_config_color(siblings)

        with transaction.atomic():
            cfg = TeamConfiguration.objects.create(
                course=course, name=name, label_prefix=label_prefix, color=color,
            )
            for t in teams:
                Team.objects.create(
                    team_configuration=cfg,
                    number=int(t['number']),
                    size=int(t['size']),
                    display_name=(t.get('display_name') or '').strip(),
                )
        return JsonResponse(_team_configuration_to_dict(cfg))
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@csrf_exempt
def update_team_configuration(request):
    """POST /team_configurations/update/  body: {id, name?, label_prefix?, color?, teams?, archived?}"""
    if request.method != 'POST':
        return HttpResponse(status=405)
    try:
        data = json.loads(request.body)
        cfg_id = data.get('id')
        if not cfg_id:
            return JsonResponse({'error': 'id required'}, status=400)
        try:
            cfg = TeamConfiguration.objects.get(id=cfg_id)
        except TeamConfiguration.DoesNotExist:
            return JsonResponse({'error': 'Configuration not found'}, status=404)

        with transaction.atomic():
            if 'name' in data and data['name']:
                new_name = data['name'].strip()
                # Dedupe against siblings (excluding self)
                sibling_names = set(
                    TeamConfiguration.objects
                    .filter(course=cfg.course).exclude(id=cfg.id).filter(archived=False)
                    .values_list('name', flat=True)
                )
                if new_name in sibling_names:
                    n = 2
                    while f'{new_name} {n}' in sibling_names:
                        n += 1
                    new_name = f'{new_name} {n}'
                cfg.name = new_name
            if 'label_prefix' in data:
                cfg.label_prefix = data['label_prefix']
            if 'color' in data and data['color'] in dict(COLOR_CHOICES):
                cfg.color = data['color']
            if 'archived' in data:
                cfg.archived = bool(data['archived'])
            cfg.save()

            if 'teams' in data:
                # Replace the teams wholesale. Preserve existing ids where number matches
                # so any downstream references don't break.
                existing_by_number = {t.number: t for t in cfg.teams.all()}
                incoming_numbers = set()
                for t in data['teams']:
                    number = int(t['number'])
                    size = int(t['size'])
                    display_name = (t.get('display_name') or '').strip()
                    incoming_numbers.add(number)
                    if number in existing_by_number:
                        team = existing_by_number[number]
                        team.size = size
                        team.display_name = display_name
                        team.save()
                    else:
                        Team.objects.create(
                            team_configuration=cfg, number=number, size=size,
                            display_name=display_name,
                        )
                # Remove teams that no longer exist
                cfg.teams.exclude(number__in=incoming_numbers).delete()

                # Propagate the new team list to every survey snapshot sourced from
                # this configuration so already-created surveys reflect the latest
                # team count/sizes/names (matches the team list students see in
                # the picker on feedback.html). Without this, edits only affect
                # newly created surveys and duplicates — past surveys keep their
                # stale snapshot even after the instructor adjusts teams.
                incoming_meta = {
                    int(t['number']): {
                        'size': int(t['size']),
                        'display_name': (t.get('display_name') or '').strip(),
                    } for t in data['teams']
                }
                for snap in cfg.snapshots.all():
                    snap_existing = {st.number: st for st in snap.teams.all()}
                    for number, meta in incoming_meta.items():
                        if number in snap_existing:
                            st = snap_existing[number]
                            dirty = False
                            if st.size != meta['size']:
                                st.size = meta['size']; dirty = True
                            if st.display_name != meta['display_name']:
                                st.display_name = meta['display_name']; dirty = True
                            if dirty:
                                st.save()
                        else:
                            SurveyTeam.objects.create(
                                snapshot=snap, number=number, size=meta['size'],
                                display_name=meta['display_name'],
                            )
                    # Remove snapshot teams that no longer exist in the source —
                    # but only when no student has self-assigned to them, so we
                    # don't cascade-delete an existing SessionTeamAssignment.
                    obsolete = snap.teams.exclude(number__in=incoming_numbers)
                    obsolete.filter(assignments__isnull=True).delete()

        return JsonResponse(_team_configuration_to_dict(cfg))
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@csrf_exempt
def archive_team_configuration(request):
    """POST /team_configurations/archive/  body: {id}"""
    if request.method != 'POST':
        return HttpResponse(status=405)
    try:
        data = json.loads(request.body)
        cfg_id = data.get('id')
        try:
            cfg = TeamConfiguration.objects.get(id=cfg_id)
        except TeamConfiguration.DoesNotExist:
            return JsonResponse({'error': 'Configuration not found'}, status=404)
        cfg.archived = True
        cfg.save()
        return JsonResponse({'ok': True})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@csrf_exempt
def delete_team_configuration(request):
    """POST /team_configurations/delete/  body: {id}
    Refuses with 409 if any snapshot references this configuration."""
    if request.method != 'POST':
        return HttpResponse(status=405)
    try:
        data = json.loads(request.body)
        cfg_id = data.get('id')
        try:
            cfg = TeamConfiguration.objects.get(id=cfg_id)
        except TeamConfiguration.DoesNotExist:
            return JsonResponse({'error': 'Configuration not found'}, status=404)
        if SurveyTeamSnapshot.objects.filter(source_configuration=cfg).exists():
            return JsonResponse({
                'error': 'configuration is referenced by surveys; archive instead',
            }, status=409)
        cfg.delete()
        return JsonResponse({'ok': True})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@csrf_exempt
def get_survey_team_snapshot(request):
    """GET /survey_team_snapshot/?survey_id=... OR public_id=..."""
    if request.method != 'GET':
        return HttpResponse(status=405)
    survey_id = request.GET.get('survey_id')
    public_id = request.GET.get('public_id')
    try:
        if public_id:
            survey = FeedbackGPT.objects.get(public_id=public_id)
        elif survey_id:
            survey = FeedbackGPT.objects.get(id=survey_id)
        else:
            return JsonResponse({'error': 'survey_id or public_id required'}, status=400)
    except FeedbackGPT.DoesNotExist:
        return JsonResponse({'error': 'Survey not found'}, status=404)
    snap = getattr(survey, 'team_snapshot', None)
    if not snap:
        return JsonResponse({'error': 'No team snapshot for this survey'}, status=404)
    return JsonResponse(_survey_snapshot_to_dict(snap))


@csrf_exempt
def assign_session_to_team(request):
    """POST /session_team_assignment/  body: {session_id, survey_team_id}. Idempotent — later calls update the existing row."""
    if request.method != 'POST':
        return HttpResponse(status=405)
    try:
        data = json.loads(request.body)
        session_id = data.get('session_id')
        team_id = data.get('survey_team_id')
        if not session_id or not team_id:
            return JsonResponse({'error': 'session_id and survey_team_id required'}, status=400)
        try:
            survey_team = SurveyTeam.objects.get(id=team_id)
        except SurveyTeam.DoesNotExist:
            return JsonResponse({'error': 'SurveyTeam not found'}, status=404)
        obj, _ = SessionTeamAssignment.objects.update_or_create(
            session_id=session_id,
            defaults={'survey_team': survey_team},
        )
        return JsonResponse({'ok': True, 'id': obj.id})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@csrf_exempt
def list_survey_team_assignments(request):
    """GET /survey_team_assignments/?survey_id=... OR public_id=..."""
    if request.method != 'GET':
        return HttpResponse(status=405)
    survey_id = request.GET.get('survey_id')
    public_id = request.GET.get('public_id')
    try:
        if public_id:
            survey = FeedbackGPT.objects.get(public_id=public_id)
        elif survey_id:
            survey = FeedbackGPT.objects.get(id=survey_id)
        else:
            return JsonResponse({'error': 'survey_id or public_id required'}, status=400)
    except FeedbackGPT.DoesNotExist:
        return JsonResponse({'error': 'Survey not found'}, status=404)
    snap = getattr(survey, 'team_snapshot', None)
    if not snap:
        return JsonResponse([], safe=False)
    assignments = SessionTeamAssignment.objects.filter(survey_team__snapshot=snap)
    return JsonResponse([_assignment_to_dict(a) for a in assignments], safe=False)


# =========================================================================
# LEAI PDF reflection ingest
# =========================================================================
# Instructor uploads PDFs of student template-style reflections in
# FeedbackAnalyzer; we parse, propose a section→prompt mapping, let the
# instructor confirm/edit, then commit as FeedbackMessage rows that blend
# into the existing analytics surface. Every commit produces a batch row
# the instructor can revert from the UI.

def _job_to_dict(job) -> dict:
    """Serialise an LEAIPdfIngestJob for the polling client."""
    from . import leai_pdf_ingest
    # Always refresh from DB — the worker thread mutates status/items
    # asynchronously and the caller's instance can be stale (especially
    # under inline-thread tests, where the worker has already finished
    # by the time the view serialises).
    job.refresh_from_db()
    # Apply stale recovery on read so a dyno-cycle zombie surfaces as
    # failed (mirrors the chat-turn / quicktake pattern).
    if leai_pdf_ingest.is_job_stale(job):
        LEAIPdfIngestJob.objects.filter(pk=job.pk).update(
            status=LEAIPdfIngestJob.STATUS_FAILED,
            error='Processing took too long. Please try again with fewer files.',
        )
        job.refresh_from_db()
    schema_body = job.survey.form_schema.body if job.survey.form_schema_id else None
    prompts = leai_pdf_ingest.flatten_prompts_from_schema(schema_body)
    return {
        'job_id': str(job.id),
        'survey_id': job.survey_id,
        'status': job.status,
        'progress': job.progress or {},
        'items': job.items or [],
        'prompts': prompts,
        'error': job.error or '',
        'created_at': job.created_at.isoformat() if job.created_at else None,
        'updated_at': job.updated_at.isoformat() if job.updated_at else None,
    }


def _batch_to_dict(batch) -> dict:
    return {
        'batch_id': str(batch.id),
        'survey_id': batch.survey_id,
        'committed_by': batch.committed_by,
        'student_count': batch.student_count,
        'message_count': batch.message_count,
        'items_summary': batch.items_summary or [],
        'reverted_at': batch.reverted_at.isoformat() if batch.reverted_at else None,
        'created_at': batch.created_at.isoformat() if batch.created_at else None,
    }


@csrf_exempt
def leai_pdf_ingest_start(request):
    """POST /api/leai_pdf_ingest/start/ — multipart upload.

    Form fields:
        survey_id: int
        attributions: JSON {filename: student_id}
        created_by: str (optional)
        files: one or more uploaded PDFs (form field name 'files')

    Returns 202 with the job descriptor (status='pending'). Worker
    runs in a background thread; client polls
    GET /api/leai_pdf_ingest/<job_id>/.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    survey_id = request.POST.get('survey_id') or request.POST.get('surveyId')
    if not survey_id:
        return JsonResponse({'error': 'survey_id is required'}, status=400)
    try:
        survey = FeedbackGPT.objects.select_related('form_schema').get(pk=int(survey_id))
    except (FeedbackGPT.DoesNotExist, ValueError, TypeError):
        return JsonResponse({'error': 'Survey not found'}, status=404)

    if survey.mode != 'form' or not survey.form_schema_id:
        return JsonResponse(
            {'error': 'PDF ingest is only available for Structured Reflection surveys.'},
            status=400,
        )

    try:
        attributions_raw = request.POST.get('attributions') or '{}'
        attributions = json.loads(attributions_raw)
        if not isinstance(attributions, dict):
            raise ValueError('attributions must be a JSON object')
    except (json.JSONDecodeError, ValueError) as e:
        return JsonResponse({'error': f'Bad attributions: {e}'}, status=400)

    uploaded = request.FILES.getlist('files')
    if not uploaded:
        return JsonResponse({'error': 'No files uploaded.'}, status=400)

    files: list[tuple[str, bytes]] = []
    for f in uploaded:
        files.append((f.name, f.read()))

    from . import leai_pdf_ingest
    try:
        job = leai_pdf_ingest.start_pdf_ingest_job(
            survey=survey,
            files=files,
            attributions={str(k): str(v) for k, v in attributions.items()},
            created_by=request.POST.get('created_by') or '',
        )
    except leai_pdf_ingest.IngestJobConflict as e:
        # 409 + the existing job descriptor so the frontend can resume
        # polling that job instead of presenting a generic error.
        return JsonResponse({
            'error': str(e),
            'existing_job': _job_to_dict(e.existing_job),
        }, status=409)
    except ValueError as e:
        return JsonResponse({'error': str(e)}, status=400)

    return JsonResponse(_job_to_dict(job), status=202)


@csrf_exempt
def leai_pdf_ingest_detail(request, job_id):
    """GET/DELETE /api/leai_pdf_ingest/<job_id>/

    GET — poll for status + items. Stale recovery applies.
    DELETE — abandon a preview without committing.
    """
    try:
        job = LEAIPdfIngestJob.objects.select_related('survey', 'survey__form_schema').get(pk=job_id)
    except LEAIPdfIngestJob.DoesNotExist:
        return JsonResponse({'error': 'Job not found'}, status=404)

    if request.method == 'GET':
        return JsonResponse(_job_to_dict(job))
    if request.method == 'DELETE':
        job.delete()
        return HttpResponse(status=204)
    return JsonResponse({'error': 'Method not allowed'}, status=405)


@csrf_exempt
def leai_pdf_ingest_commit(request, job_id):
    """POST /api/leai_pdf_ingest/<job_id>/commit/

    Body:
        {items: [{filename, student_id, mapping, skip}], dedup_decisions: {sid: 'replace'|'skip'|'add'}, committed_by: str}

    Writes FeedbackMessage rows + creates an LEAIPdfIngestBatch.
    Invalidates the course's Quick Takes. Deletes the preview job.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    try:
        job = LEAIPdfIngestJob.objects.select_related('survey', 'survey__form_schema').get(pk=job_id)
    except LEAIPdfIngestJob.DoesNotExist:
        return JsonResponse({'error': 'Job not found'}, status=404)
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    items = data.get('items') or []
    dedup = data.get('dedup_decisions') or {}
    committed_by = data.get('committed_by') or ''
    if not isinstance(items, list) or not isinstance(dedup, dict):
        return JsonResponse({'error': 'items must be a list, dedup_decisions a dict'}, status=400)

    from . import leai_pdf_ingest
    try:
        batch = leai_pdf_ingest.commit_pdf_ingest_job(
            job=job,
            confirmed_items=items,
            dedup_decisions={str(k): str(v) for k, v in dedup.items()},
            committed_by=str(committed_by),
        )
    except ValueError as e:
        return JsonResponse({'error': str(e)}, status=400)

    return JsonResponse({
        'batch': _batch_to_dict(batch),
        'committed_count': batch.message_count,
        'students_affected': batch.student_count,
    })


@csrf_exempt
def leai_pdf_ingest_roster(request):
    """GET /api/leai_pdf_ingest/roster/?survey_id=<id>

    Returns the per-survey roster the upload UI uses to attribute PDFs.
    Combines:
      1. Students who've submitted to any survey in this course (so the
         instructor sees the natural roster, not just the current survey).
      2. Students who already have PDF responses on this specific
         survey (used to flag dedup before commit).

    Response:
        {
          'students': [{'student_id': str, 'submitted_to_this_survey': bool,
                        'has_pdf_on_this_survey': bool, 'submission_count': int}],
        }
    """
    if request.method != 'GET':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    survey_id = request.GET.get('survey_id')
    if not survey_id:
        return JsonResponse({'error': 'survey_id is required'}, status=400)
    try:
        survey = FeedbackGPT.objects.select_related('course').get(pk=int(survey_id))
    except (FeedbackGPT.DoesNotExist, ValueError, TypeError):
        return JsonResponse({'error': 'Survey not found'}, status=404)

    course = survey.course
    course_surveys = FeedbackGPT.objects.filter(course=course).values_list('id', flat=True) if course else [survey.id]
    # Anonymous string ids are fine for our roster — the analyzer is
    # already instructor-only and shows the same ids in the response
    # browser. We exclude blank/None ids.
    course_msgs = (
        FeedbackMessage.objects
        .filter(gpt_id__in=list(course_surveys))
        .exclude(student_id__in=['', None])
        .values('student_id', 'gpt_id', 'source')
    )
    by_student: dict[str, dict] = {}
    for row in course_msgs:
        sid = row['student_id']
        rec = by_student.setdefault(sid, {
            'student_id': sid,
            'submitted_to_this_survey': False,
            'has_pdf_on_this_survey': False,
            'submission_count': 0,
        })
        rec['submission_count'] += 1
        if int(row['gpt_id']) == int(survey.id):
            rec['submitted_to_this_survey'] = True
            if row['source'] == FeedbackMessage.SOURCE_PDF:
                rec['has_pdf_on_this_survey'] = True

    # Sort: this-survey submitters first, then by id
    students = sorted(
        by_student.values(),
        key=lambda r: (not r['submitted_to_this_survey'], r['student_id']),
    )
    return JsonResponse({'students': students})


@csrf_exempt
def leai_pdf_ingest_dedup_check(request):
    """POST /api/leai_pdf_ingest/dedup_check/

    Body: {survey_id: int, student_ids: [str]}
    Returns: {existing: [str]} — subset already having PDF responses.

    Used by the commit modal to surface which students already have
    PDF responses so the instructor can pick replace/skip/add per
    student. Pure read, no side effects.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    survey_id = data.get('survey_id')
    student_ids = data.get('student_ids') or []
    if not survey_id or not isinstance(student_ids, list):
        return JsonResponse({'error': 'survey_id + student_ids required'}, status=400)
    try:
        survey = FeedbackGPT.objects.get(pk=int(survey_id))
    except (FeedbackGPT.DoesNotExist, ValueError, TypeError):
        return JsonResponse({'error': 'Survey not found'}, status=404)
    from . import leai_pdf_ingest
    existing = leai_pdf_ingest.detect_existing_pdf_students(
        survey=survey,
        student_ids=[str(s) for s in student_ids if s],
    )
    return JsonResponse({'existing': existing})


@csrf_exempt
def leai_pdf_ingest_batches_list(request):
    """GET /api/leai_pdf_ingest_batches/?survey_id=<id>[&include_reverted=1]

    Lists ingest batches for a survey, newest first. Reverted batches
    are excluded by default; pass include_reverted=1 to include them
    (with a `reverted_at` timestamp on the row).
    """
    if request.method != 'GET':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    survey_id = request.GET.get('survey_id')
    if not survey_id:
        return JsonResponse({'error': 'survey_id is required'}, status=400)
    qs = LEAIPdfIngestBatch.objects.filter(survey_id=survey_id)
    if request.GET.get('include_reverted') not in ('1', 'true', 'yes'):
        qs = qs.filter(reverted_at__isnull=True)
    return JsonResponse([_batch_to_dict(b) for b in qs.order_by('-created_at')[:100]], safe=False)


@csrf_exempt
def leai_pdf_ingest_batch_revert(request, batch_id):
    """POST /api/leai_pdf_ingest_batches/<batch_id>/revert/

    Hard-deletes the batch's FeedbackMessage rows, marks the batch
    reverted, invalidates Quick Take. Idempotent — calling on an
    already-reverted batch is a 409.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    try:
        batch = LEAIPdfIngestBatch.objects.select_related('survey').get(pk=batch_id)
    except LEAIPdfIngestBatch.DoesNotExist:
        return JsonResponse({'error': 'Batch not found'}, status=404)
    if batch.reverted_at:
        return JsonResponse({'error': 'Batch already reverted', 'batch': _batch_to_dict(batch)}, status=409)
    from . import leai_pdf_ingest
    deleted = leai_pdf_ingest.revert_pdf_ingest_batch(batch)
    batch.refresh_from_db()
    return JsonResponse({'deleted_count': deleted, 'batch': _batch_to_dict(batch)})
