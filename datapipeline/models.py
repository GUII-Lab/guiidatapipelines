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
    SOURCE_CHAT = 'chat'
    SOURCE_PDF = 'pdf'
    SOURCE_CHOICES = [(SOURCE_CHAT, 'Chat'), (SOURCE_PDF, 'PDF')]

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
    # Distinguishes chat-collected responses (default) from instructor-
    # ingested PDF reflections. Frontend renders a 📄 badge when 'pdf'.
    source = models.CharField(
        max_length=8, choices=SOURCE_CHOICES, default=SOURCE_CHAT,
    )
    # When source='pdf', points to the ingest batch that created this row.
    # SET_NULL on batch delete so manifest can be archived without
    # cascading the responses (revert uses explicit bulk delete instead).
    pdf_batch = models.ForeignKey(
        'LEAIPdfIngestBatch', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='messages',
    )

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

    MODE_CHOICES = [
        ('general', 'General course feedback'),
        ('group', 'In-group team feedback'),
        ('form', 'Form-mapped structured reflection'),
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

    # In-Group feedback mode. Existing surveys remain 'general' by default.
    mode = models.CharField(max_length=16, choices=MODE_CHOICES, default='general')

    # When mode='form', binds the survey to a FormSchema row. The schema body
    # (sections, prompts, probes) is loaded by feedback.html on session start.
    # Null for general/group surveys.
    form_schema = models.ForeignKey(
        'FormSchema', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='surveys',
    )

    def __str__(self):
        return self.name


class FormSchema(models.Model):
    """A structured-reflection schema (sections, prompts, probes, fields).

    Stored in DB so instructors / staff can revise without a redeploy. Surveys
    in mode='form' bind to one schema via FeedbackGPT.form_schema. The body is
    free-form JSON consumed by leai-formmode.js + leaiInsights.
    """
    schema_id = models.CharField(max_length=64, unique=True)
    version = models.CharField(max_length=16, default='1.0.0')
    title = models.CharField(max_length=200, default='')
    course_label = models.CharField(max_length=100, blank=True, default='')
    week_number = models.IntegerField(null=True, blank=True)
    body = models.JSONField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['course_label', 'week_number', 'schema_id']

    def __str__(self):
        return f'{self.schema_id} ({self.title})'


# ================= In-Group feedback models =================
# Team configurations are per-course groupings of teams (e.g. "Lab Teams" for
# weeks 1-4, "Final Project Teams" for weeks 5-10). A survey in group mode
# snapshots one configuration at creation time so later edits to the source
# configuration don't retroactively change past surveys.

COLOR_CHOICES = [
    ('forest', 'forest'), ('plum', 'plum'), ('amber', 'amber'),
    ('teal', 'teal'), ('rose', 'rose'), ('indigo', 'indigo'),
    ('brown', 'brown'), ('slate', 'slate'),
]


class TeamConfiguration(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='team_configurations')
    name = models.CharField(max_length=100)
    label_prefix = models.CharField(max_length=50, default='Team')
    color = models.CharField(max_length=16, choices=COLOR_CHOICES, default='forest')
    archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('course', 'name')]
        ordering = ['created_at']

    def __str__(self):
        return f'{self.name} ({self.course.course_id})'


class Team(models.Model):
    team_configuration = models.ForeignKey(
        TeamConfiguration, on_delete=models.CASCADE, related_name='teams',
    )
    number = models.IntegerField()
    size = models.IntegerField()
    # Optional instructor-supplied label that overrides "<label_prefix> <number>"
    # in student-facing UI and the analyzer (e.g., sponsor team names in HCI 271).
    display_name = models.CharField(max_length=100, blank=True, default='')

    class Meta:
        unique_together = [('team_configuration', 'number')]
        ordering = ['number']

    def __str__(self):
        return f'{self.team_configuration.label_prefix} {self.number} (size {self.size})'


class SurveyTeamSnapshot(models.Model):
    """Per-survey team structure captured at survey creation.

    `name`, `label_prefix`, and `color` are frozen at creation (renaming or
    recoloring the source TeamConfiguration does not propagate). The
    `teams` (number + size) DO follow the source: when an instructor edits
    teams in update_team_configuration, every snapshot tied to that source
    is synced — adds/resizes propagate, and obsolete numbers are removed
    only when no SessionTeamAssignment references them, so existing student
    team picks are preserved. Cascaded deletes from FeedbackGPT remove the
    snapshot and its SurveyTeam rows; assignments cascade with SurveyTeam.
    """

    survey = models.OneToOneField(
        FeedbackGPT, on_delete=models.CASCADE, related_name='team_snapshot',
    )
    source_configuration = models.ForeignKey(
        TeamConfiguration, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='snapshots',
    )
    name = models.CharField(max_length=100)
    label_prefix = models.CharField(max_length=50, default='Team')
    color = models.CharField(max_length=16, choices=COLOR_CHOICES, default='forest')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Snapshot of {self.name} for survey {self.survey_id}'


class SurveyTeam(models.Model):
    snapshot = models.ForeignKey(
        SurveyTeamSnapshot, on_delete=models.CASCADE, related_name='teams',
    )
    number = models.IntegerField()
    size = models.IntegerField()
    # Frozen at snapshot creation but synced with source Team.display_name on
    # update_team_configuration (same as size). Empty string means "no custom
    # name — show <label_prefix> <number>".
    display_name = models.CharField(max_length=100, blank=True, default='')

    class Meta:
        unique_together = [('snapshot', 'number')]
        ordering = ['number']

    def __str__(self):
        return f'{self.snapshot.label_prefix} {self.number} (size {self.size})'


class SessionTeamAssignment(models.Model):
    """Records which team a student self-identified as when they opened an
    in-group survey. session_id is the anonymous session identifier already
    used by FeedbackMessage (plain CharField, no FK) — we intentionally do
    NOT reference student identity.
    """

    session_id = models.CharField(max_length=100, unique=True)
    survey_team = models.ForeignKey(
        SurveyTeam, on_delete=models.CASCADE, related_name='assignments',
    )
    assigned_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'session {self.session_id[:8]}… -> team {self.survey_team.number}'


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
    """One turn in a Feedback Chat session.

    Assistant messages are generated asynchronously: the turn endpoint
    saves a placeholder with status=pending and a worker thread populates
    text/cited then flips status to ready (or failed). User and system
    messages are always written with status=ready.
    """

    ROLE_CHOICES = [
        ('user', 'user'),
        ('assistant', 'assistant'),
        ('system', 'system'),
    ]

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

    session = models.ForeignKey(
        LEAIChatSession, on_delete=models.CASCADE, related_name='messages',
    )
    role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    text = models.TextField(blank=True)
    cited = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_READY,
    )
    error = models.TextField(blank=True, default='')
    job_started_at = models.DateTimeField(null=True, blank=True)
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
    # Phase 5: disagreements (tensions) and noticeable absences (gaps).
    # Older rows have default empty arrays; the frontend renders them
    # only when present.
    tensions = models.JSONField(default=list, blank=True)
    gaps = models.JSONField(default=list, blank=True)
    # Phase 7: per-team rollup, populated only when the scope contains
    # group-mode survey responses. Same nullable-empty contract as
    # tensions/gaps so older rows continue to deserialize.
    team_health = models.JSONField(default=list, blank=True)
    # Phase 8: per-form-section rollup, populated only when the scope
    # contains form-mode (mode='form') survey responses.
    form_sections = models.JSONField(default=list, blank=True)
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


class LEAIPdfIngestBatch(models.Model):
    """Permanent manifest of a committed PDF ingest batch.

    Created by `commit/` after the instructor confirms a mapping. Holds
    only counts + a per-PDF summary for audit; the actual response rows
    live in FeedbackMessage with `pdf_batch` set to this batch.

    Revert sets `reverted_at` and bulk-deletes the linked FeedbackMessage
    rows. The batch row itself stays for audit, with the manifest intact.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    survey = models.ForeignKey(
        'FeedbackGPT', on_delete=models.CASCADE,
        related_name='pdf_ingest_batches',
    )
    committed_by = models.CharField(max_length=100, blank=True, default='')
    student_count = models.IntegerField(default=0)
    message_count = models.IntegerField(default=0)
    # Snapshot per ingested PDF: {filename, student_id, status, prompt_count}
    items_summary = models.JSONField(default=list)
    reverted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['survey', '-created_at'])]

    def __str__(self):
        state = ' (reverted)' if self.reverted_at else ''
        return f'PdfBatch {self.survey_id} · {self.student_count} students{state}'


class LEAIPdfIngestJob(models.Model):
    """Transient PDF-ingest preview job.

    Created by `start/`, populated by a worker thread, polled by the
    instructor's UI. Lives until `commit/` consumes it (worker deletes
    the row) or stale-recovery auto-fails it.

    `items` is a list of dicts:
        {filename, student_id,
         status: 'ok'|'low_conf'|'failed',
         extracted_text, mapping: {prompt_id: text},
         low_conf_prompts: [prompt_id], error: str}
    """

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

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    survey = models.ForeignKey(
        'FeedbackGPT', on_delete=models.CASCADE,
        related_name='pdf_ingest_jobs',
    )
    created_by = models.CharField(max_length=100, blank=True, default='')
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING,
    )
    items = models.JSONField(default=list)
    progress = models.JSONField(default=dict)
    error = models.TextField(blank=True, default='')
    job_started_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['survey', '-created_at'])]

    def __str__(self):
        return f'PdfIngestJob {self.id} · {self.status}'
