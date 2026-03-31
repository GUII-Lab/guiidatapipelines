from django.http import JsonResponse, HttpResponseBadRequest,HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils.dateparse import parse_datetime
from .models import *  # Ensure this is your custom User model
import json
import os
import secrets
import string
from collections import defaultdict
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
            gpt = FeedbackGPT.objects.create(
                name=data.get('name', ''),
                instructions=data.get('instructions', ''),
                created_by=data.get('instructor_name', ''),
                course=course,
                week_number=data.get('week_number'),
                survey_label=data.get('survey_label', ''),
                public_id=_generate_public_id(),
            )
            return JsonResponse({'status': 'success', 'id': gpt.id, 'public_id': gpt.public_id, 'name': gpt.name})
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
        gpts = FeedbackGPT.objects.filter(course=course).order_by('week_number', 'created_at').values(
            'id', 'public_id', 'name', 'week_number', 'survey_label', 'instructions', 'created_at'
        )
        return JsonResponse(list(gpts), safe=False)
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
        return JsonResponse({
            'id': gpt.id,
            'public_id': gpt.public_id,
            'name': gpt.name,
            'instructions': gpt.instructions,
            'week_number': gpt.week_number,
            'survey_label': gpt.survey_label,
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
            result.append({
                'gpt_id': gpt.id,
                'name': gpt.name,
                'week_number': gpt.week_number,
                'survey_label': gpt.survey_label,
                'sessions': dict(sessions),
                'session_count': len(sessions),
            })
        return JsonResponse(result, safe=False)
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


