from django.http import JsonResponse, HttpResponseBadRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from .models import *  # Ensure this is your custom User model
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
            )
            feedback_message.save()
            return JsonResponse({'status': 'success', 'message': 'Feedback message saved successfully'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)})


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
                password=data.get('password', ''),
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
            if course.password == password:
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
                themes=data.get('themes', []),
                timing_category=data.get('timing_category', ''),
                anonymity_mode=data.get('anonymity_mode', 'anonymous'),
                reporting_structure=data.get('reporting_structure', ''),
                survey_type=data.get('survey_type', 'individual'),
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
                'themes': gpt.themes,
                'timing_category': gpt.timing_category,
                'anonymity_mode': gpt.anonymity_mode,
                'reporting_structure': gpt.reporting_structure,
                'survey_type': gpt.survey_type,
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
            'survey_type': gpt.survey_type,
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
                         'themes', 'timing_category', 'anonymity_mode',
                         'reporting_structure', 'survey_type']
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
                themes=src.themes,
                timing_category=src.timing_category,
                anonymity_mode=src.anonymity_mode,
                reporting_structure=src.reporting_structure,
                survey_type=src.survey_type,
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
        writer.writerow(['session_id', 'sent_by', 'content', 'created_at'])
        for m in messages:
            writer.writerow([m.session_id, m.sent_by, m.content, m.created_at.strftime('%Y-%m-%d %H:%M:%S')])

        filename = f'survey_{gpt.id}_responses.csv'
        response = HttpResponse(buf.getvalue(), content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
    return HttpResponse(status=405)


@csrf_exempt
def get_group_session(request):
    """
    Return or create a canonical session_id for a group code.
    Group sessions share a session_id so all members see the same conversation.
    GET ?group_code=<code>&survey_id=<id> → { session_id }
    """
    if request.method == 'GET':
        group_code = request.GET.get('group_code', '').strip().upper()
        survey_id = request.GET.get('survey_id')
        if not group_code or not survey_id:
            return JsonResponse({'error': 'group_code and survey_id required'}, status=400)
        # Use a deterministic session_id: "group_<survey_id>_<group_code>"
        session_id = f'group_{survey_id}_{group_code}'
        return JsonResponse({'session_id': session_id})
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
    """Endpoint to get AI response from OpenAI using chat history and user text"""
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            chat_history = data.get('chat_history', [])
            user_text = data.get('user_text')
            
            if not user_text:
                return JsonResponse({'error': 'user_text is required'}, status=400)
            
            # Get OpenAI API key from environment
            openai_key = os.environ.get('oaiKey')
            if not openai_key:
                return JsonResponse({'error': 'OpenAI API key not configured'}, status=500)
            
            # Format messages for OpenAI API
            messages = []
            
            # Add chat history if provided
            if chat_history:
                for msg in chat_history:
                    # Handle different formats of chat history
                    if isinstance(msg, dict):
                        role = msg.get('role', 'user')
                        content = msg.get('content', msg.get('text', ''))
                        # Map sent_by to role if present
                        if 'sent_by' in msg:
                            sent_by = msg['sent_by'].lower()
                            if sent_by == 'user' or sent_by == 'student':
                                role = 'user'
                            elif sent_by == 'assistant' or sent_by == 'gpt' or sent_by == 'ai':
                                role = 'assistant'
                            content = msg.get('content', '')
                        messages.append({'role': role, 'content': content})
            
            # Add the current user message
            messages.append({'role': 'user', 'content': user_text})
            
            # Make request to OpenAI API
            url = "https://api.openai.com/v1/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {openai_key}"
            }
            payload = {
                "model": "gpt-4o",
                "messages": messages
            }
            
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            
            if response.status_code == 200:
                response_data = response.json()
                ai_message = response_data['choices'][0]['message']['content']
                return JsonResponse({
                    'status': 'success',
                    'response': ai_message,
                    'usage': response_data.get('usage', {})
                })
            else:
                return JsonResponse({
                    'error': f'OpenAI API error: {response.status_code}',
                    'details': response.text
                }, status=response.status_code)
                
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)
        except Exception as e:
            return JsonResponse({'error': f'Error: {str(e)}'}, status=500)
    else:
        return JsonResponse({'error': 'Only POST method allowed'}, status=405)


