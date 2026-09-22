"""One backend-owned course action policy for the React instructor API."""

from leai.models import CourseAccessRestriction, CourseMembership, InstitutionMembership


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


def allowed_course_actions(account, course):
    if (
        not account.is_active
        or not account.user.is_active
        or account.must_change_password
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
    if roles & TEACHER_ROLES:
        return ACTIONS

    researcher_membership_ids = list(
        memberships.filter(role="researcher").values_list("id", flat=True)
    )
    inherited = bool(researcher_membership_ids) and not CourseAccessRestriction.objects.filter(
        course=course,
        institution_membership_id__in=researcher_membership_ids,
        denied=True,
    ).exists()
    if inherited:
        return ACTIONS
    if "ta" in roles:
        return tuple(action for action in ACTIONS if action in TA_ACTIONS)
    return ()


def has_course_action(account, course, action):
    return action in ACTIONS and action in allowed_course_actions(account, course)
