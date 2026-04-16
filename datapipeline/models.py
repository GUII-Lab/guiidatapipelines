import uuid

from django.db import models
from django.utils import timezone


class User(models.Model):
    username = models.CharField(max_length=100)
    email = models.EmailField(unique=True)
    university = models.CharField(max_length=100)
    assigned_id = models.CharField(max_length=100)
    joined_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.username

class Message(models.Model):
    session_id = models.CharField(max_length=100)
    student_id = models.CharField(max_length=100)
    sent_by = models.CharField(max_length = 20)
    created_at = models.DateTimeField(auto_now_add=True)
    content = models.TextField()
    gpt_used = models.CharField(max_length=100)

    def __str__(self):
        return f"{self.student_id} used {self.gpt_used}"

class FeedbackMessage(models.Model):
    session_id = models.CharField(max_length=100)
    student_id = models.CharField(max_length=100)
    sent_by = models.CharField(max_length = 20)
    created_at = models.DateTimeField(auto_now_add=True)
    content = models.TextField()
    gpt_used = models.CharField(max_length=100)
    gpt_id = models.IntegerField(null=True, blank=True)
    # Per-message research consent flag. The student opts in/out via the
    # consent modal in feedback.html; the frontend sends this with each
    # message. When False, the message must NOT be used for any GUII Lab
    # research analysis (Privacy Policy §5).
    research_consent = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.student_id} used {self.gpt_used}"


class Course(models.Model):
    course_id = models.SlugField(max_length=50, unique=True)
    course_name = models.CharField(max_length=200)
    instructor_name = models.CharField(max_length=100)
    password = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.course_name} ({self.course_id})"


class FeedbackGPT(models.Model):
    ANONYMITY_CHOICES = [
        ('anonymous', 'Anonymous'),
        ('pseudonymous', 'Pseudonymous'),
        ('identified', 'Identified'),
    ]

    id = models.AutoField(primary_key=True)
    public_id = models.CharField(max_length=16, unique=True, blank=True, default='')
    name = models.CharField(max_length=100)
    created_by = models.CharField(max_length=100, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    instructions = models.TextField()
    updated_at = models.DateTimeField(auto_now=True)
    course = models.ForeignKey(Course, on_delete=models.SET_NULL, null=True, blank=True, related_name='surveys')
    week_number = models.IntegerField(null=True, blank=True)
    survey_label = models.CharField(max_length=200, blank=True, default='')

    # Lifecycle fields
    expires_at = models.DateTimeField(null=True, blank=True)
    opens_at = models.DateTimeField(null=True, blank=True)
    is_closed = models.BooleanField(default=False)

    # Metadata fields
    anonymity_mode = models.CharField(max_length=20, choices=ANONYMITY_CHOICES, default='anonymous')
    reporting_structure = models.CharField(max_length=100, blank=True, default='')
    canvas_integration = models.BooleanField(default=False)

    def __str__(self):
        return self.name


class CustomGPT(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=100)
    created_by = models.CharField(max_length=100, default='Sai')
    university = models.CharField(max_length=100, default='UCSC')
    gpt_type = models.CharField(max_length=50,default='')
    instructions = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class FireData(models.Model):
    data = models.JSONField()


class Image(models.Model):
    image = models.ImageField(upload_to='images/')
    title = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title or f"Image {self.id}"

    @property
    def image_url(self):
        if self.image:
            return self.image.url
        return None


class LEAIChatSession(models.Model):
    """Persisted Feedback Chat session, scoped to a Course."""

    SCOPE_CHOICES = [
        ('course', 'course'),
        ('week', 'week'),
        ('custom', 'custom'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name='leai_chat_sessions',
    )
    title = models.CharField(max_length=120, default='New chat')
    scope_kind = models.CharField(
        max_length=16, choices=SCOPE_CHOICES, default='course',
    )
    scope_week_number = models.IntegerField(null=True, blank=True)
    scope_survey_ids = models.JSONField(default=list, blank=True)
    scope_session_ids = models.JSONField(default=list, blank=True)
    system_prompt_override = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        indexes = [models.Index(fields=['course', '-updated_at'])]

    def __str__(self):
        return f'{self.title} ({self.course.course_id})'


class LEAIChatMessage(models.Model):
    """One turn in a Feedback Chat session."""

    ROLE_CHOICES = [
        ('user', 'user'),
        ('assistant', 'assistant'),
        ('system', 'system'),
    ]

    session = models.ForeignKey(
        LEAIChatSession, on_delete=models.CASCADE, related_name='messages',
    )
    role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    text = models.TextField()
    cited = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        indexes = [models.Index(fields=['session', 'created_at'])]

    def __str__(self):
        return f'{self.role}: {self.text[:60]}'


class LEAIQuickTake(models.Model):
    """Persisted AI Quick Take, cached per (course, scope_key)."""

    STATUS_PENDING = 'pending'
    STATUS_RUNNING = 'running'
    STATUS_READY = 'ready'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_RUNNING, 'Running'),
        (STATUS_READY, 'Ready'),
        (STATUS_FAILED, 'Failed'),
    ]

    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name='leai_quicktakes',
    )
    scope_key = models.CharField(max_length=64)
    bullets = models.JSONField(default=list)
    verification = models.JSONField(default=list)
    system_prompt = models.TextField()
    user_text = models.TextField()
    model_name = models.CharField(max_length=64, default='')
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_READY,
    )
    error = models.TextField(blank=True, default='')
    job_started_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('course', 'scope_key')]
        indexes = [models.Index(fields=['course', 'scope_key'])]

    def __str__(self):
        return f'QuickTake {self.scope_key} ({self.course.course_id})'
