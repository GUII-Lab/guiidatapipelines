"""One backend-owned course action policy for the React instructor API."""

from collections import defaultdict

from django.db.models import Q

from leai.models import Course, CourseAccessRestriction, CourseMembership, InstitutionMembership


ACTIONS = (
    "course.manage",
    "feedback.author",
    "feedback.publish",
    "responses.view",
    "responses.export",
    "analysis.use",
)
TA_ACTIONS = frozenset(("feedback.author", "responses.view", "analysis.use"))
TEACHER_ROLES = frozenset(("owner", "instructor"))


def _actions_for_grants(roles, researcher_inherited):
    if roles & TEACHER_ROLES or researcher_inherited:
        return ACTIONS
    if "ta" in roles:
        return tuple(action for action in ACTIONS if action in TA_ACTIONS)
    return ()


def allowed_course_actions(account, course):
    if (
        not account.is_active
        or not account.user.is_active
        or course.lifecycle_state != "active"
    ):
        return ()
    if account.platform_role == "platform_admin":
        return ACTIONS

    memberships = InstitutionMembership.objects.filter(
        account=account,
        institution_id=course.institution_id,
        is_active=True,
    )
    membership_ids = list(memberships.values_list("id", flat=True))
    if not membership_ids:
        return ()

    roles = set(
        CourseMembership.objects.filter(
            course=course,
            institution_membership_id__in=membership_ids,
        ).values_list("role", flat=True)
    )
    researcher_membership_ids = list(
        memberships.filter(role="researcher").values_list("id", flat=True)
    )
    inherited = bool(researcher_membership_ids) and not CourseAccessRestriction.objects.filter(
        course=course,
        institution_membership_id__in=researcher_membership_ids,
        denied=True,
    ).exists()
    return _actions_for_grants(roles, inherited)


def has_course_action(account, course, action):
    return action in ACTIONS and action in allowed_course_actions(account, course)


def has_researcher_course_access(account, course):
    """Require an active institution Researcher grant, including explicit course grants."""
    if (
        not account.is_active
        or not account.user.is_active
        or course.lifecycle_state != "active"
    ):
        return False
    researcher_ids = list(
        InstitutionMembership.objects.filter(
            account=account,
            institution_id=course.institution_id,
            role="researcher",
            is_active=True,
        ).values_list("id", flat=True)
    )
    if not researcher_ids:
        return False
    explicit = CourseMembership.objects.filter(
        course=course,
        institution_membership_id__in=researcher_ids,
    ).exists()
    inherited = not CourseAccessRestriction.objects.filter(
        course=course,
        institution_membership_id__in=researcher_ids,
        denied=True,
    ).exists()
    return explicit or inherited


def accessible_course_rows(account, *, course_id=None):
    """Resolve list DTO grants in batches, without a query per Course."""
    if (
        not account.is_active
        or not account.user.is_active
    ):
        return []
    courses = Course.objects.filter(lifecycle_state="active").select_related("institution")
    if course_id is not None:
        courses = courses.filter(public_id=course_id)
    if account.platform_role == "platform_admin":
        courses = list(courses.order_by("name", "id"))
        explicit = defaultdict(set)
        for course_pk, role in CourseMembership.objects.filter(
            course_id__in=[course.pk for course in courses],
            institution_membership__account=account,
            institution_membership__is_active=True,
        ).values_list("course_id", "role"):
            explicit[course_pk].add(role)
        return [
            (
                course,
                ACTIONS,
                next(
                    (role for role in ("owner", "instructor", "ta") if role in explicit[course.pk]),
                    "platform_admin",
                ),
            )
            for course in courses
        ]

    memberships = list(
        InstitutionMembership.objects.filter(account=account, is_active=True).values(
            "id", "institution_id", "role"
        )
    )
    if not memberships:
        return []
    membership_ids = [row["id"] for row in memberships]
    explicit_course_ids = CourseMembership.objects.filter(
        institution_membership_id__in=membership_ids,
    ).values("course_id")
    researchers = {
        row["institution_id"]: row["id"]
        for row in memberships
        if row["role"] == "researcher"
    }
    if researchers:
        restricted_course_ids = CourseAccessRestriction.objects.filter(
            institution_membership_id__in=researchers.values(),
            denied=True,
        ).values("course_id")
        courses = courses.filter(
            Q(pk__in=explicit_course_ids)
            | (
                Q(institution_id__in=researchers)
                & ~Q(pk__in=restricted_course_ids)
            )
        )
    else:
        courses = courses.filter(pk__in=explicit_course_ids)
    courses = list(courses.order_by("name", "id"))
    if not courses:
        return []

    course_ids = [course.pk for course in courses]
    roles_by_course = defaultdict(set)
    for course_pk, role in CourseMembership.objects.filter(
        course_id__in=course_ids,
        institution_membership_id__in=membership_ids,
    ).values_list("course_id", "role"):
        roles_by_course[course_pk].add(role)

    restrictions = set(
        CourseAccessRestriction.objects.filter(
            course_id__in=course_ids,
            institution_membership_id__in=researchers.values(),
            denied=True,
        ).values_list("course_id", "institution_membership_id")
    ) if researchers else set()

    rows = []
    for course in courses:
        roles = roles_by_course[course.pk]
        researcher_id = researchers.get(course.institution_id)
        inherited = researcher_id is not None and (course.pk, researcher_id) not in restrictions
        actions = _actions_for_grants(roles, inherited)
        if not actions:
            continue
        role = next((value for value in ("owner", "instructor", "ta") if value in roles), "researcher")
        rows.append((course, actions, role))
    return rows
