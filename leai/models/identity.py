import uuid

from django.conf import settings
from django.db import models


class Institution(models.Model):
    id = models.BigAutoField(primary_key=True)
    slug = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=200)


class InstructorAccount(models.Model):
    class PlatformRole(models.TextChoices):
        MEMBER = "member"
        PLATFORM_ADMIN = "platform_admin"

    id = models.BigAutoField(primary_key=True)
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    email = models.EmailField(unique=True)
    display_name = models.CharField(max_length=100)
    platform_role = models.CharField(
        max_length=32,
        choices=PlatformRole.choices,
        default=PlatformRole.MEMBER,
    )
    is_active = models.BooleanField(default=True)
    # Kept as an inert compatibility field for older account rows and API clients.
    must_change_password = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=models.Q(platform_role__in=["member", "platform_admin"]),
                name="leai_instructor_account_platform_role_valid",
            ),
        ]


class Course(models.Model):
    class Lifecycle(models.TextChoices):
        ACTIVE = "active"
        COMPLETED = "completed"
        ARCHIVED = "archived"

    class BannerDisplayMode(models.TextChoices):
        PERSISTENT = "persistent"
        TIMED = "timed"

    class BannerSplitMode(models.TextChoices):
        PERCENTAGE = "percentage"
        COUNT = "count"

    id = models.BigAutoField(primary_key=True)
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    institution = models.ForeignKey(
        Institution,
        on_delete=models.PROTECT,
        related_name="courses",
    )
    course_code = models.SlugField(max_length=100)
    name = models.CharField(max_length=200)
    lifecycle_state = models.CharField(
        max_length=16,
        choices=Lifecycle.choices,
        default=Lifecycle.ACTIVE,
    )
    settings_version = models.PositiveIntegerField(default=1)
    analysis_data_version = models.PositiveBigIntegerField(default=0)
    banner_enabled = models.BooleanField(default=False)
    banner_text = models.TextField(blank=True, default="")
    banner_dismissible = models.BooleanField(default=False)
    banner_display_mode = models.CharField(
        max_length=16,
        choices=BannerDisplayMode.choices,
        default=BannerDisplayMode.PERSISTENT,
    )
    banner_duration_seconds = models.PositiveIntegerField(default=10)
    banner_split_enabled = models.BooleanField(default=False)
    banner_split_mode = models.CharField(
        max_length=16,
        choices=BannerSplitMode.choices,
        default=BannerSplitMode.PERCENTAGE,
    )
    banner_split_value = models.PositiveIntegerField(default=50)
    assistant_display_name = models.CharField(max_length=100, blank=True, default="")
    referral_enabled = models.BooleanField(default=False)
    referral_text = models.CharField(max_length=200, blank=True, default="")
    completion_certificate_enabled_by_default = models.BooleanField(default=False)
    completed_response_download_enabled_by_default = models.BooleanField(default=False)
    student_debug_enabled = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=models.Q(lifecycle_state__in=["active", "completed", "archived"]),
                name="leai_course_lifecycle_state_valid",
            ),
            models.UniqueConstraint(
                fields=("institution", "course_code"),
                name="leai_course_code_per_institution_uniq",
            ),
            models.CheckConstraint(
                check=(
                    models.Q(banner_display_mode="persistent")
                    | models.Q(
                        banner_display_mode="timed",
                        banner_duration_seconds__gt=0,
                    )
                ),
                name="leai_course_timed_banner_duration_positive",
            ),
            models.CheckConstraint(
                check=(
                    models.Q(
                        banner_split_mode="percentage",
                        banner_split_value__gte=0,
                        banner_split_value__lte=100,
                    )
                    | models.Q(
                        banner_split_mode="count",
                        banner_split_value__gt=0,
                    )
                ),
                name="leai_course_banner_split_value_valid",
            ),
        ]


class InstitutionMembership(models.Model):
    class Role(models.TextChoices):
        INSTRUCTOR = "instructor"
        RESEARCHER = "researcher"

    account = models.ForeignKey(
        InstructorAccount,
        on_delete=models.PROTECT,
        related_name="institution_memberships",
    )
    institution = models.ForeignKey(
        Institution,
        on_delete=models.PROTECT,
        related_name="instructor_memberships",
    )
    role = models.CharField(max_length=16, choices=Role.choices)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=models.Q(role__in=["instructor", "researcher"]),
                name="leai_institution_membership_role_valid",
            ),
            models.UniqueConstraint(
                fields=("account", "institution"),
                name="leai_institution_membership_account_institution_uniq",
            ),
        ]


class InstructorSession(models.Model):
    account = models.ForeignKey(
        InstructorAccount,
        on_delete=models.PROTECT,
        related_name="sessions",
    )
    capability_digest = models.CharField(max_length=128, unique=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)


class InstructorLoginThrottle(models.Model):
    """Shared short-window counters keyed by HMAC, never raw email or IP."""

    id = models.BigAutoField(primary_key=True)
    key_digest = models.CharField(max_length=64, unique=True)
    window_start = models.DateTimeField(db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=models.Q(attempts__lte=120),
                name="leai_login_throttle_attempts_bounded",
            ),
        ]


class CourseMembership(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner"
        INSTRUCTOR = "instructor"
        TA = "ta"

    course = models.ForeignKey(
        Course,
        on_delete=models.PROTECT,
        related_name="memberships",
    )
    institution_membership = models.ForeignKey(
        InstitutionMembership,
        on_delete=models.PROTECT,
        related_name="course_memberships",
    )
    role = models.CharField(max_length=16, choices=Role.choices)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=models.Q(role__in=["owner", "instructor", "ta"]),
                name="leai_course_membership_role_valid",
            ),
            models.UniqueConstraint(
                fields=("course", "institution_membership"),
                name="leai_course_membership_course_institution_membership_uniq",
            ),
        ]


class CourseAccessRestriction(models.Model):
    course = models.ForeignKey(
        Course,
        on_delete=models.PROTECT,
        related_name="access_restrictions",
    )
    institution_membership = models.ForeignKey(
        InstitutionMembership,
        on_delete=models.PROTECT,
        related_name="course_access_restrictions",
    )
    denied = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("course", "institution_membership"),
                name="leai_course_access_restriction_course_membership_uniq",
            ),
        ]
