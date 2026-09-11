import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
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
    # True on the ONE AI turn where the POINT TO A HUMAN referral gate fired.
    # The engine strips the [REFERRED] marker before display and before the
    # content is persisted, so this flag is the only surviving record that the
    # nudge happened. Latched per-conversation in engine state; per-message
    # here, so at most one AI row per session should carry it. Also read back
    # on resume so the latch survives a refresh.
    referred = models.BooleanField(default=False)
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
    # Exact Form Mode question attribution. These fields are nullable so
    # historical chat/PDF rows remain valid. The student's original content
    # stays authoritative; this metadata only identifies which schema field
    # was on screen when they wrote it.
    form_schema_id = models.CharField(max_length=100, null=True, blank=True)
    form_schema_version = models.CharField(max_length=40, null=True, blank=True)
    form_section_id = models.CharField(max_length=100, null=True, blank=True)
    form_field_id = models.CharField(max_length=100, null=True, blank=True)
    form_field_label = models.TextField(null=True, blank=True)
    form_response_phase = models.CharField(max_length=16, null=True, blank=True)
    response_session = models.ForeignKey(
        'ResponseSession',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='messages',
    )
    sequence = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(response_session__isnull=True, sequence__isnull=True)
                    | models.Q(response_session__isnull=False, sequence__isnull=False)
                ),
                name='leai_message_session_sequence_pair',
            ),
            models.UniqueConstraint(
                fields=['response_session', 'sequence'],
                condition=models.Q(response_session__isnull=False),
                name='leai_unique_session_message_sequence',
            ),
        ]

    def __str__(self):
        return f"{self.student_id} used {self.gpt_used}"


class Institution(models.Model):
    slug = models.SlugField(max_length=100, unique=True)
    name = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name', 'slug']

    def save(self, *args, **kwargs):
        self.slug = (self.slug or '').strip().lower()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class InstructorAccount(models.Model):
    AUTH_MANUAL = 'manual'
    AUTH_COGNITO = 'cognito'
    AUTH_PROVIDER_CHOICES = [
        (AUTH_MANUAL, 'Manual'),
        (AUTH_COGNITO, 'Amazon Cognito'),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='leai_instructor_account',
    )
    email = models.EmailField(unique=True)
    display_name = models.CharField(max_length=200)
    auth_provider = models.CharField(
        max_length=20,
        choices=AUTH_PROVIDER_CHOICES,
        default=AUTH_MANUAL,
    )
    external_subject = models.CharField(
        max_length=255,
        unique=True,
        null=True,
        blank=True,
    )
    email_verified_at = models.DateTimeField(null=True, blank=True)
    must_change_password = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['email']

    def save(self, *args, **kwargs):
        self.email = (self.email or '').strip().lower()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.email


class InstitutionMembership(models.Model):
    ROLE_MEMBER = 'member'
    ROLE_ADMIN = 'admin'
    ROLE_CHOICES = [
        (ROLE_MEMBER, 'Member'),
        (ROLE_ADMIN, 'Institution administrator'),
    ]

    institution = models.ForeignKey(
        Institution,
        on_delete=models.PROTECT,
        related_name='memberships',
    )
    instructor = models.ForeignKey(
        InstructorAccount,
        on_delete=models.PROTECT,
        related_name='institution_memberships',
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_MEMBER)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['institution', 'instructor'],
                name='leai_unique_institution_instructor',
            ),
        ]
        ordering = ['institution__name', 'instructor__email']

    def __str__(self):
        return f'{self.instructor.email} at {self.institution.name}'


class InstructorSession(models.Model):
    instructor = models.ForeignKey(
        InstructorAccount,
        on_delete=models.CASCADE,
        related_name='sessions',
    )
    token_digest = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['instructor', 'revoked_at', 'expires_at']),
        ]

    @property
    def is_valid(self):
        return (
            self.revoked_at is None
            and self.expires_at > timezone.now()
            and self.instructor.is_active
            and self.instructor.user.is_active
        )


class Course(models.Model):
    BANNER_DISPLAY_CHOICES = [
        ('persistent', 'Persistent'),
        ('timed', 'Auto-dismiss after duration'),
    ]
    BANNER_SPLIT_CHOICES = [
        ('percentage', 'Percentage of students'),
        ('count', 'Fixed number of students'),
    ]

    course_id = models.SlugField(max_length=50, unique=True)
    course_name = models.CharField(max_length=200)
    instructor_name = models.CharField(max_length=100)
    password = models.CharField(max_length=100)
    institution = models.ForeignKey(
        Institution,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='courses',
    )
    legacy_password_login_enabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    # Course-wide student notification banner. One banner per course; shown on
    # every student survey (general/group/form) when enabled. Settings travel
    # with the course, not the survey, so the prompt builder is untouched.
    banner_enabled = models.BooleanField(default=False)
    banner_text = models.TextField(
        blank=True, default='',
        help_text="Message shown to students. Leave blank to use the default disclaimer.",
    )
    banner_dismissible = models.BooleanField(
        default=False,
        help_text="Whether students can close the banner.",
    )
    banner_display_mode = models.CharField(
        max_length=16, choices=BANNER_DISPLAY_CHOICES, default='persistent',
    )
    banner_duration_seconds = models.PositiveIntegerField(
        default=10,
        help_text="Seconds before auto-dismiss when display mode is 'timed'.",
    )

    # A/B split. When off, every survey viewer sees the banner (subject to
    # banner_enabled). When on, only an assigned subset sees it and the rest
    # are the silent control arm; assignment is persisted per anonymous session
    # in BannerAssignment. Everything is anonymous, so the split is random.
    banner_split_enabled = models.BooleanField(
        default=False,
        help_text="When on, only a random subset of students see the banner (A/B).",
    )
    banner_split_mode = models.CharField(
        max_length=16, choices=BANNER_SPLIT_CHOICES, default='percentage',
    )
    banner_split_value = models.PositiveIntegerField(
        default=50,
        help_text="Percent (0-100) in percentage mode, or number of students in count mode.",
    )

    # Course-level display name for the AI assistant. Replaces the default
    # "LEAI" tag on AI message bubbles in the student chat. Travels with the
    # course (not the survey), so every survey in the course shares it. Blank
    # falls back to "LEAI".
    bot_display_name = models.CharField(
        max_length=100, blank=True, default='',
        help_text="Name shown on the AI's message tag in student chats. Blank uses the default 'LEAI'.",
    )

    # POINT TO A HUMAN referral gate. When enabled, the form-mode engine adds a
    # per-turn gate letting the bot point a struggling student to a human, once
    # per conversation, without troubleshooting or promising outcomes. Travels
    # with the course. The destination wording is configurable so a course with
    # no standing office hours can name another channel; blank falls back to
    # the engine default ("your instructor or TA during their office hours").
    referral_enabled = models.BooleanField(
        default=False,
        help_text="Let the chat point struggling students to a human, once per conversation.",
    )
    referral_text = models.CharField(
        max_length=200, blank=True, default='',
        help_text="Where the bot points them. Blank uses the default 'your instructor or TA during their office hours'.",
    )

    # CROSS-WEEK TRACKING. When enabled, the student page silently records
    # per-session device signals (persistent browser key + fingerprint) in
    # SessionIdentity so the analyzer can cluster the same student's sessions
    # across the course's weekly surveys. No name is ever collected; disclosure
    # lives in the survey terms text. Off by default for every course.
    identity_tracking_enabled = models.BooleanField(
        default=False,
        help_text="Silently link one student's sessions across weeks via device signals.",
    )

    completion_certificate_enabled = models.BooleanField(default=False)
    parsed_document_download_enabled = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.course_name} ({self.course_id})"


class InstructorAuditEvent(models.Model):
    ACTION_LOGIN_SUCCEEDED = 'auth.login_succeeded'
    ACTION_LOGIN_DENIED = 'auth.login_denied'
    ACTION_LOGOUT = 'auth.logout'
    ACTION_PASSWORD_CHANGED = 'account.password_changed'
    ACTION_PROFILE_UPDATED = 'account.profile_updated'
    ACTION_COURSE_CREATED = 'course.created'
    ACTION_COURSE_BANNER_UPDATED = 'course.banner_updated'
    ACTION_COURSE_CUSTOMIZATION_UPDATED = 'course.customization_updated'
    ACTION_SURVEY_CREATED = 'survey.created'
    ACTION_SURVEY_UPDATED = 'survey.updated'
    ACTION_SURVEY_STATUS_CHANGED = 'survey.status_changed'
    ACTION_SURVEY_CLONED = 'survey.cloned'
    ACTION_SURVEY_DELETED = 'survey.deleted'
    ACTION_SURVEY_RESPONSES_EXPORTED = 'survey.responses_exported'
    ACTION_ANALYSIS_SESSION_CREATED = 'analysis.session_created'
    ACTION_ANALYSIS_SESSION_UPDATED = 'analysis.session_updated'
    ACTION_ANALYSIS_SESSION_DELETED = 'analysis.session_deleted'
    ACTION_ANALYSIS_TURN_STARTED = 'analysis.turn_started'
    ACTION_ANALYSIS_QUICKTAKE_GENERATED = 'analysis.quicktake_generated'
    ACTION_ANALYSIS_QUICKTAKE_DELETED = 'analysis.quicktake_deleted'
    ACTION_TEAM_CONFIGURATION_CREATED = 'team_configuration.created'
    ACTION_TEAM_CONFIGURATION_UPDATED = 'team_configuration.updated'
    ACTION_TEAM_CONFIGURATION_ARCHIVED = 'team_configuration.archived'
    ACTION_TEAM_CONFIGURATION_DELETED = 'team_configuration.deleted'
    ACTION_PDF_INGEST_STARTED = 'pdf_ingest.started'
    ACTION_PDF_INGEST_ABANDONED = 'pdf_ingest.abandoned'
    ACTION_PDF_INGEST_COMMITTED = 'pdf_ingest.committed'
    ACTION_PDF_INGEST_REVERTED = 'pdf_ingest.reverted'
    ACTION_AUTHORIZATION_DENIED = 'authorization.denied'

    ACTION_CHOICES = [
        (ACTION_LOGIN_SUCCEEDED, 'Login succeeded'),
        (ACTION_LOGIN_DENIED, 'Login denied'),
        (ACTION_LOGOUT, 'Logout'),
        (ACTION_PASSWORD_CHANGED, 'Password changed'),
        (ACTION_PROFILE_UPDATED, 'Profile updated'),
        (ACTION_COURSE_CREATED, 'Course created'),
        (ACTION_COURSE_BANNER_UPDATED, 'Course banner updated'),
        (ACTION_COURSE_CUSTOMIZATION_UPDATED, 'Course customization updated'),
        (ACTION_SURVEY_CREATED, 'Survey created'),
        (ACTION_SURVEY_UPDATED, 'Survey updated'),
        (ACTION_SURVEY_STATUS_CHANGED, 'Survey status changed'),
        (ACTION_SURVEY_CLONED, 'Survey cloned'),
        (ACTION_SURVEY_DELETED, 'Survey deleted'),
        (ACTION_SURVEY_RESPONSES_EXPORTED, 'Survey responses exported'),
        (ACTION_ANALYSIS_SESSION_CREATED, 'Analysis session created'),
        (ACTION_ANALYSIS_SESSION_UPDATED, 'Analysis session updated'),
        (ACTION_ANALYSIS_SESSION_DELETED, 'Analysis session deleted'),
        (ACTION_ANALYSIS_TURN_STARTED, 'Analysis turn started'),
        (ACTION_ANALYSIS_QUICKTAKE_GENERATED, 'Analysis quick take generated'),
        (ACTION_ANALYSIS_QUICKTAKE_DELETED, 'Analysis quick take deleted'),
        (ACTION_TEAM_CONFIGURATION_CREATED, 'Team configuration created'),
        (ACTION_TEAM_CONFIGURATION_UPDATED, 'Team configuration updated'),
        (ACTION_TEAM_CONFIGURATION_ARCHIVED, 'Team configuration archived'),
        (ACTION_TEAM_CONFIGURATION_DELETED, 'Team configuration deleted'),
        (ACTION_PDF_INGEST_STARTED, 'PDF ingest started'),
        (ACTION_PDF_INGEST_ABANDONED, 'PDF ingest abandoned'),
        (ACTION_PDF_INGEST_COMMITTED, 'PDF ingest committed'),
        (ACTION_PDF_INGEST_REVERTED, 'PDF ingest reverted'),
        (ACTION_AUTHORIZATION_DENIED, 'Authorization denied'),
    ]

    OUTCOME_SUCCESS = 'success'
    OUTCOME_DENIED = 'denied'
    OUTCOME_FAILED = 'failed'
    OUTCOME_CHOICES = [
        (OUTCOME_SUCCESS, 'Success'),
        (OUTCOME_DENIED, 'Denied'),
        (OUTCOME_FAILED, 'Failed'),
    ]

    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    occurred_at = models.DateTimeField(auto_now_add=True)
    actor = models.ForeignKey(
        InstructorAccount,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='audit_events',
    )
    session = models.ForeignKey(
        InstructorSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_events',
    )
    course = models.ForeignKey(
        Course,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='instructor_audit_events',
    )
    course_id_snapshot = models.CharField(max_length=50, blank=True, default='')
    action = models.CharField(max_length=64, choices=ACTION_CHOICES)
    outcome = models.CharField(max_length=16, choices=OUTCOME_CHOICES)
    target_type = models.CharField(max_length=32, blank=True, default='')
    target_id = models.CharField(max_length=100, blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-occurred_at', '-id']
        indexes = [
            models.Index(
                fields=['actor', '-occurred_at'],
                name='leai_audit_actor_time',
            ),
            models.Index(
                fields=['course', '-occurred_at'],
                name='leai_audit_course_time',
            ),
            models.Index(
                fields=['action', 'outcome', '-occurred_at'],
                name='leai_audit_action_time',
            ),
        ]


class CourseMembership(models.Model):
    ROLE_OWNER = 'owner'
    ROLE_INSTRUCTOR = 'instructor'
    ROLE_TA = 'ta'
    ROLE_CHOICES = [
        (ROLE_OWNER, 'Owner'),
        (ROLE_INSTRUCTOR, 'Instructor'),
        (ROLE_TA, 'Teaching assistant'),
    ]

    course = models.ForeignKey(
        Course,
        on_delete=models.CASCADE,
        related_name='memberships',
    )
    institution_membership = models.ForeignKey(
        InstitutionMembership,
        on_delete=models.PROTECT,
        related_name='course_memberships',
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    can_publish = models.BooleanField(default=False)
    can_export = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['course', 'institution_membership'],
                name='leai_unique_course_institution_membership',
            ),
            models.UniqueConstraint(
                fields=['course'],
                condition=models.Q(role='owner', is_active=True),
                name='leai_one_active_course_owner',
            ),
        ]
        ordering = ['course__course_id', 'role', 'institution_membership_id']

    def clean(self):
        super().clean()
        if not self.course_id or not self.institution_membership_id:
            return
        if self.course.institution_id is None:
            raise ValidationError({'course': 'Course must belong to an institution.'})
        if self.course.institution_id != self.institution_membership.institution_id:
            raise ValidationError({
                'institution_membership': (
                    'Course membership must belong to the course institution.'
                ),
            })

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.institution_membership.instructor.email}: {self.course.course_id} ({self.role})'


class LegacyCourseOwnershipReview(models.Model):
    STATE_LINKED = 'linked'
    STATE_AMBIGUOUS = 'ambiguous'
    STATE_UNRESOLVED = 'unresolved'
    STATE_CHOICES = [
        (STATE_LINKED, 'Linked'),
        (STATE_AMBIGUOUS, 'Ambiguous'),
        (STATE_UNRESOLVED, 'Unresolved'),
    ]

    course = models.OneToOneField(
        Course,
        on_delete=models.CASCADE,
        related_name='ownership_review',
    )
    state = models.CharField(max_length=20, choices=STATE_CHOICES)
    linked_membership = models.ForeignKey(
        CourseMembership,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='legacy_ownership_reviews',
    )
    notes = models.TextField(blank=True, default='')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        super().clean()
        if self.state == self.STATE_LINKED and self.linked_membership_id is None:
            raise ValidationError({'linked_membership': 'Linked reviews require a membership.'})
        if self.linked_membership_id and self.linked_membership.course_id != self.course_id:
            raise ValidationError({
                'linked_membership': 'Ownership membership must belong to the reviewed course.',
            })

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class BannerAssignment(models.Model):
    """Per-session record of whether the course banner was shown to an
    anonymous student session.

    Created on first survey load when the A/B split is on. Persisting the
    assignment keeps it stable across reloads (a student never flips between
    seeing/not-seeing) and serves as the research exposure log — joinable to
    FeedbackMessage by session_id to compare the treatment and control arms.
    """

    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name='banner_assignments',
    )
    session_id = models.CharField(max_length=100)
    shown = models.BooleanField()
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('course', 'session_id')]
        indexes = [models.Index(fields=['course', 'session_id'])]

    def __str__(self):
        return f'{self.course.course_id}/{self.session_id[:8]}… shown={self.shown}'


class SessionIdentity(models.Model):
    """Per-session device signals for cross-week student clustering.

    Written once per anonymous survey session (when the course has
    identity_tracking_enabled). device_key is a persistent localStorage UUID —
    stable on one browser, lost on storage clears. fingerprint is a FingerprintJS
    visitor id — survives storage clears, drifts on browser updates, and can
    collide on identical lab machines. The analyzer clusters sessions sharing
    either signal (fingerprint links are demoted when a fingerprint spans many
    device keys, i.e. a shared machine). Neither value identifies a person.
    """

    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name='session_identities',
    )
    session_id = models.CharField(max_length=100)
    device_key = models.CharField(max_length=64, blank=True, default='')
    fingerprint = models.CharField(max_length=64, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('course', 'session_id')]
        indexes = [
            models.Index(fields=['course', 'device_key']),
            models.Index(fields=['course', 'fingerprint']),
        ]

    def __str__(self):
        return f'{self.course.course_id}/{self.session_id[:8]}… dev={self.device_key[:8]}'


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


class ResponseSession(models.Model):
    SOURCE_STUDENT = 'student'
    SOURCE_PDF = 'pdf'
    SOURCE_CHOICES = [
        (SOURCE_STUDENT, 'Student chat'),
        (SOURCE_PDF, 'PDF import'),
    ]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    course = models.ForeignKey(
        Course,
        on_delete=models.PROTECT,
        related_name='response_sessions',
    )
    survey = models.ForeignKey(
        FeedbackGPT,
        on_delete=models.CASCADE,
        related_name='response_sessions',
    )
    client_session_id = models.CharField(max_length=100)
    source = models.CharField(
        max_length=16,
        choices=SOURCE_CHOICES,
        default=SOURCE_STUDENT,
    )
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    next_message_sequence = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['survey', 'client_session_id'],
                name='leai_unique_survey_client_session',
            ),
        ]
        indexes = [
            models.Index(
                fields=['course', '-started_at'],
                name='leai_response_course_time',
            ),
        ]

    def clean(self):
        super().clean()
        if not self.course_id or not self.survey_id:
            return
        if self.survey.course_id != self.course_id:
            raise ValidationError({
                'course': 'Response session course must match the survey course.',
            })

    def __str__(self):
        return f'{self.survey_id}/{self.client_session_id[:8]}…'


class SurveyCompletionCertificate(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    survey = models.ForeignKey(
        FeedbackGPT,
        on_delete=models.CASCADE,
        related_name='completion_certificates',
    )
    session_id = models.CharField(max_length=100)
    code = models.CharField(max_length=19, unique=True)
    issued_at = models.DateTimeField(auto_now_add=True)
    progress_snapshot = models.JSONField(default=dict)
    display_snapshot = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['survey', 'session_id'],
                name='unique_completion_certificate_per_survey_session',
            ),
        ]
        indexes = [models.Index(fields=['survey', 'code'])]

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
    # Phase 9: prescriptive "suggested actions" — instructor-facing
    # recommendations derived from the feedback (friction to address,
    # mid-course adjustments). Descriptive fields above say what students
    # said; this says what the instructor might do about it. Empty when
    # there's nothing clearly actionable.
    actions = models.JSONField(default=list, blank=True)
    # Number of responses in scope at the moment this Quick Take was
    # generated. The UI compares it against the current response count to
    # show "N new responses not yet included" so instructors know when a
    # regenerate is worthwhile. 0 on older rows predating this field.
    responses_count_at_generation = models.IntegerField(default=0)
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
