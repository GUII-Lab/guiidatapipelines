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
            )
            return JsonResponse({
                'status': 'success',
                'id': gpt.id,
                'public_id': gpt.public_id,
                'name': gpt.name,
                'expires_at': gpt.expires_at.isoformat() if gpt.expires_at else None,
            })
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
            result.append({
                'id': gpt.id,
                'public_id': gpt.public_id,
                'name': gpt.name,
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

        return JsonResponse({
            'id': gpt.id,
            'public_id': gpt.public_id,
            'name': gpt.name,
            'instructions': gpt.instructions,
            'week_number': gpt.week_number,
            'survey_label': gpt.survey_label,
            'is_active': is_active,
            'reason': reason,
            'expires_at': gpt.expires_at.isoformat() if gpt.expires_at else None,
            'opens_at': gpt.opens_at.isoformat() if gpt.opens_at else None,
            'anonymity_mode': gpt.anonymity_mode,
            'canvas_integration': gpt.canvas_integration,
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
                })
            session_count = len(sessions)
            msg_count = messages.count()
            avg_turns = round(msg_count / session_count) if session_count else 0
            result.append({
                'gpt_id': gpt.id,
                'name': gpt.name,
                'week_number': gpt.week_number,
                'survey_label': gpt.survey_label,
                'sessions': dict(sessions),
                'session_count': session_count,
                'avg_turns': avg_turns,
                'is_closed': gpt.is_closed,
                'expires_at': gpt.expires_at.isoformat() if gpt.expires_at else None,
                'opens_at': gpt.opens_at.isoformat() if gpt.opens_at else None,
                'canvas_integration': gpt.canvas_integration,
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

            gpt.save()
            return JsonResponse({'status': 'success', 'id': gpt.id})
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
            )
            return JsonResponse({
                'status': 'success',
                'id': clone.id,
                'public_id': clone.public_id,
                'name': clone.name,
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


# ---------------------------------------------------------------------------
# LEAI Chat Session helpers
# ---------------------------------------------------------------------------

def _session_detail_response(session, status=200):
    """Serialize a LEAIChatSession (with messages) to a JsonResponse."""
    messages = list(
        session.messages.order_by('created_at').values(
            'id', 'role', 'text', 'cited', 'created_at'
        )
    )
    for m in messages:
        m['created_at'] = m['created_at'].isoformat()

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
    }, status=status)


def _quicktake_to_dict(qt):
    """Serialize a LEAIQuickTake instance to a plain dict."""
    return {
        'id': qt.pk,
        'course_id': qt.course.course_id,
        'scope_key': qt.scope_key,
        'bullets': qt.bullets,
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
    """POST /api/leai_chat_sessions/<uuid>/turn/"""
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
        assistant_msg = leai_analysis.run_chat_turn(session, user_text)
    except ValueError as e:
        return JsonResponse({'error': str(e)}, status=400)
    except openai_client.OpenAIRefusalError as e:
        return JsonResponse({'error': e.detail}, status=422)
    except openai_client.OpenAIClientError as e:
        return JsonResponse({'error': e.detail}, status=e.status_code)

    session.refresh_from_db()
    return JsonResponse({
        'message': {
            'id': assistant_msg.pk,
            'role': assistant_msg.role,
            'text': assistant_msg.text,
            'cited': assistant_msg.cited,
            'created_at': assistant_msg.created_at.isoformat(),
        },
        'session_updated_at': session.updated_at.isoformat(),
    })


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

