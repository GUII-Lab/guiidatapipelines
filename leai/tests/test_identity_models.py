import queue
import threading

from django.contrib.auth import get_user_model
from django.db import (
    DatabaseError,
    IntegrityError,
    close_old_connections,
    connection,
    connections,
    transaction,
)
from django.db.models import F
from django.test import TestCase, TransactionTestCase

from leai.models import (
    Course,
    CourseAccessRestriction,
    CourseMembership,
    Institution,
    InstitutionMembership,
    InstructorAccount,
)


class IdentityModelTests(TestCase):
    def setUp(self):
        self.institution_a = Institution.objects.create(
            slug="ucsc",
            name="UC Santa Cruz",
        )
        self.institution_b = Institution.objects.create(
            slug="ucd",
            name="UC Davis",
        )
        self.user_a = get_user_model().objects.create_user(
            username="mimi",
            email="mirapopo@ucsc.edu",
        )
        self.user_b = get_user_model().objects.create_user(
            username="ulia",
            email="uzaman@ucd.edu",
        )
        self.account_a = InstructorAccount.objects.create(
            user=self.user_a,
            email="mirapopo@ucsc.edu",
            display_name="Mimi Rapoport",
        )
        self.account_b = InstructorAccount.objects.create(
            user=self.user_b,
            email="uzaman@ucd.edu",
            display_name="Ulia Zaman",
        )
        self.membership_a = InstitutionMembership.objects.create(
            account=self.account_a,
            institution=self.institution_a,
            role="instructor",
        )
        self.membership_b = InstitutionMembership.objects.create(
            account=self.account_b,
            institution=self.institution_b,
            role="researcher",
        )
        self.course_a = Course.objects.create(
            institution=self.institution_a,
            course_code="winter-game-design",
            name="Winter Game Design",
        )

    def test_course_belongs_to_exactly_one_institution(self):
        institution = Institution.objects.create(
            slug="ucsc-course",
            name="UC Santa Cruz Course",
        )
        course = Course.objects.create(
            institution=institution,
            public_id="11111111-1111-4111-8111-111111111111",
            course_code="winter-game-design",
            name="Winter Game Design",
            lifecycle_state=Course.Lifecycle.ACTIVE,
        )

        self.assertEqual(course.institution, institution)

    def test_one_account_has_one_membership_per_institution(self):
        user = get_user_model().objects.create_user(
            username="mimi-membership",
            email="mirapopo-membership@ucsc.edu",
        )
        account = InstructorAccount.objects.create(
            user=user,
            email="mirapopo-membership@ucsc.edu",
            display_name="Mimi Rapoport",
        )
        institution = Institution.objects.create(
            slug="ucsc-membership",
            name="UC Santa Cruz Membership",
        )
        InstitutionMembership.objects.create(
            account=account,
            institution=institution,
            role="instructor",
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            InstitutionMembership.objects.create(
                account=account,
                institution=institution,
                role="researcher",
            )

    def test_course_membership_rejects_different_institution_on_create(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            CourseMembership.objects.create(
                course=self.course_a,
                institution_membership=self.membership_b,
                role="instructor",
            )

    def test_course_membership_rejects_different_institution_on_update(self):
        membership = CourseMembership.objects.create(
            course=self.course_a,
            institution_membership=self.membership_a,
            role="owner",
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            CourseMembership.objects.filter(pk=membership.pk).update(
                institution_membership=self.membership_b,
            )

    def test_course_access_restriction_rejects_different_institution_on_create(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            CourseAccessRestriction.objects.create(
                course=self.course_a,
                institution_membership=self.membership_b,
                denied=True,
            )

    def test_course_access_restriction_rejects_different_institution_on_update(self):
        restriction = CourseAccessRestriction.objects.create(
            course=self.course_a,
            institution_membership=self.membership_a,
            denied=True,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            CourseAccessRestriction.objects.filter(pk=restriction.pk).update(
                institution_membership=self.membership_b,
            )

    def test_course_institution_rejects_bulk_update_that_breaks_membership(self):
        CourseMembership.objects.create(
            course=self.course_a,
            institution_membership=self.membership_a,
            role="owner",
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            Course.objects.filter(pk=self.course_a.pk).update(
                institution=self.institution_b,
            )

    def test_course_institution_rejects_bulk_update_that_breaks_restriction(self):
        CourseAccessRestriction.objects.create(
            course=self.course_a,
            institution_membership=self.membership_a,
            denied=True,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            Course.objects.filter(pk=self.course_a.pk).update(
                institution=self.institution_b,
            )

    def test_membership_institution_rejects_bulk_update_that_breaks_membership(self):
        CourseMembership.objects.create(
            course=self.course_a,
            institution_membership=self.membership_a,
            role="owner",
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            InstitutionMembership.objects.filter(pk=self.membership_a.pk).update(
                institution=self.institution_b,
            )

    def test_membership_institution_rejects_bulk_update_that_breaks_restriction(self):
        CourseAccessRestriction.objects.create(
            course=self.course_a,
            institution_membership=self.membership_a,
            denied=True,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            InstitutionMembership.objects.filter(pk=self.membership_a.pk).update(
                institution=self.institution_b,
            )

    def test_course_institution_allows_bulk_update_without_dependents(self):
        course = Course.objects.create(
            institution=self.institution_a,
            course_code="movable-course",
            name="Movable Course",
        )

        updated = Course.objects.filter(pk=course.pk).update(
            institution=self.institution_b,
        )

        course.refresh_from_db()
        self.assertEqual(updated, 1)
        self.assertEqual(course.institution, self.institution_b)

    def test_membership_institution_allows_bulk_update_without_dependents(self):
        institution_c = Institution.objects.create(
            slug="ucla",
            name="UC Los Angeles",
        )
        membership = InstitutionMembership.objects.create(
            account=self.account_a,
            institution=institution_c,
            role="instructor",
        )

        updated = InstitutionMembership.objects.filter(pk=membership.pk).update(
            institution=self.institution_b,
        )

        membership.refresh_from_db()
        self.assertEqual(updated, 1)
        self.assertEqual(membership.institution, self.institution_b)

    def test_platform_role_rejects_direct_invalid_value(self):
        user = get_user_model().objects.create_user(
            username="invalid-platform-role",
            email="invalid-platform-role@ucsc.edu",
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            InstructorAccount.objects.create(
                user=user,
                email="invalid-platform-role@ucsc.edu",
                display_name="Invalid Platform Role",
                platform_role="invalid",
            )

    def test_institution_membership_role_rejects_direct_invalid_value(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            InstitutionMembership.objects.create(
                account=self.account_a,
                institution=self.institution_b,
                role="invalid",
            )

    def test_course_lifecycle_rejects_direct_invalid_value(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Course.objects.create(
                institution=self.institution_b,
                course_code="invalid-lifecycle",
                name="Invalid Lifecycle",
                lifecycle_state="invalid",
            )

    def test_course_membership_role_rejects_direct_invalid_value(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            CourseMembership.objects.create(
                course=self.course_a,
                institution_membership=self.membership_a,
                role="invalid",
            )

    def test_postgresql_defaults_generate_public_ids_for_sql_inserts(self):
        user = get_user_model().objects.create_user(
            username="jiahong",
            email="jli906@ucsc.edu",
        )
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO leai_instructoraccount
                    (user_id, email, display_name, platform_role, is_active, must_change_password)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING public_id
                """,
                [
                    user.pk,
                    "jli906@ucsc.edu",
                    "Jiahong Li",
                    "platform_admin",
                    True,
                    False,
                ],
            )
            account_public_id = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO leai_course
                    (institution_id, course_code, name, lifecycle_state,
                     settings_version, analysis_data_version, banner_enabled,
                     banner_text, banner_dismissible, banner_display_mode,
                     banner_duration_seconds, banner_split_enabled,
                     banner_split_mode, banner_split_value,
                     assistant_display_name, referral_enabled, referral_text,
                     completion_certificate_enabled_by_default,
                     completed_response_download_enabled_by_default,
                     anonymous_matching_enabled, student_debug_enabled)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING public_id
                """,
                [
                    self.institution_b.pk,
                    "spring-hci",
                    "Spring HCI",
                    "active",
                    1,
                    0,
                    False,
                    "",
                    False,
                    "persistent",
                    10,
                    False,
                    "percentage",
                    50,
                    "",
                    False,
                    "",
                    False,
                    False,
                    False,
                    False,
                ],
            )
            course_public_id = cursor.fetchone()[0]

        self.assertIsNotNone(account_public_id)
        self.assertIsNotNone(course_public_id)

    def test_public_ids_reject_bulk_updates(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            InstructorAccount.objects.filter(pk=self.account_a.pk).update(
                public_id="22222222-2222-4222-8222-222222222222",
            )

        with self.assertRaises(IntegrityError), transaction.atomic():
            Course.objects.filter(pk=self.course_a.pk).update(
                public_id="33333333-3333-4333-8333-333333333333",
            )

    def test_timed_banner_rejects_nonpositive_duration(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Course.objects.create(
                institution=self.institution_b,
                course_code="timed-banner",
                name="Timed Banner",
                banner_display_mode="timed",
                banner_duration_seconds=0,
            )

    def test_percentage_banner_split_rejects_value_over_100(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Course.objects.create(
                institution=self.institution_b,
                course_code="percentage-split",
                name="Percentage Split",
                banner_split_mode="percentage",
                banner_split_value=101,
            )

    def test_count_banner_split_rejects_nonpositive_value(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Course.objects.create(
                institution=self.institution_b,
                course_code="count-split",
                name="Count Split",
                banner_split_mode="count",
                banner_split_value=0,
            )


class IdentityConcurrencyTests(TransactionTestCase):
    thread_timeout_seconds = 5

    def setUp(self):
        self.institution_a = Institution.objects.create(
            slug="ucsc-concurrency",
            name="UC Santa Cruz Concurrency",
        )
        self.institution_b = Institution.objects.create(
            slug="ucd-concurrency",
            name="UC Davis Concurrency",
        )
        self.account_a = InstructorAccount.objects.create(
            user=get_user_model().objects.create_user(
                username="mimi-concurrency",
                email="mimi-concurrency@ucsc.edu",
            ),
            email="mimi-concurrency@ucsc.edu",
            display_name="Mimi Concurrency",
        )
        self.account_b = InstructorAccount.objects.create(
            user=get_user_model().objects.create_user(
                username="ulia-concurrency",
                email="ulia-concurrency@ucd.edu",
            ),
            email="ulia-concurrency@ucd.edu",
            display_name="Ulia Concurrency",
        )
        self.membership_a = InstitutionMembership.objects.create(
            account=self.account_a,
            institution=self.institution_a,
            role="instructor",
        )
        self.membership_b = InstitutionMembership.objects.create(
            account=self.account_b,
            institution=self.institution_b,
            role="researcher",
        )

    def _assert_parent_update_rejects_racing_child_write(
        self,
        *,
        parent_kind,
        child_kind,
        child_write_kind,
    ):
        parent_course = Course.objects.create(
            institution=self.institution_a,
            course_code=f"race-parent-{parent_kind}-{child_kind}-{child_write_kind}",
            name=f"Race Parent {parent_kind} {child_kind} {child_write_kind}",
        )
        parent_membership = self.membership_a
        relation = None
        source_course = None
        source_membership = None

        if child_write_kind == "create":
            target_course = parent_course
            target_membership = parent_membership
        elif parent_kind == "course":
            source_course = Course.objects.create(
                institution=self.institution_a,
                course_code=f"race-source-{child_kind}",
                name=f"Race Source {child_kind}",
            )
            target_course = parent_course
            target_membership = self.membership_a
            relation = self._create_child_relation(
                child_kind,
                course=source_course,
                institution_membership=target_membership,
            )
        else:
            source_membership = InstitutionMembership.objects.create(
                account=InstructorAccount.objects.create(
                    user=get_user_model().objects.create_user(
                        username=f"source-{child_kind}",
                        email=f"source-{child_kind}@ucsc.edu",
                    ),
                    email=f"source-{child_kind}@ucsc.edu",
                    display_name=f"Source {child_kind}",
                ),
                institution=self.institution_a,
                role="instructor",
            )
            target_course = parent_course
            target_membership = parent_membership
            relation = self._create_child_relation(
                child_kind,
                course=target_course,
                institution_membership=source_membership,
            )

        parent_updated = threading.Event()
        child_attempted = threading.Event()
        child_write_completed = threading.Event()
        allow_parent_commit = threading.Event()
        allow_child_commit = threading.Event()
        outcomes = queue.Queue()

        def parent_update():
            database_pid = None
            try:
                close_old_connections()
                database = connections["default"]
                database.ensure_connection()
                database_pid = database.connection.get_backend_pid()
                with transaction.atomic():
                    if parent_kind == "course":
                        Course.objects.filter(pk=parent_course.pk).update(
                            institution=self.institution_b,
                        )
                    else:
                        InstitutionMembership.objects.filter(
                            pk=parent_membership.pk,
                        ).update(institution=self.institution_b)
                    parent_updated.set()
                    if not allow_parent_commit.wait(self.thread_timeout_seconds * 2):
                        raise RuntimeError("test did not release the parent transaction")
                outcomes.put(("parent", "committed", database_pid))
            except Exception as error:
                outcomes.put(("parent", "error", database_pid, repr(error)))
            finally:
                close_old_connections()

        def child_create():
            database_pid = None
            try:
                close_old_connections()
                database = connections["default"]
                database.ensure_connection()
                database_pid = database.connection.get_backend_pid()
                with transaction.atomic():
                    if not parent_updated.wait(self.thread_timeout_seconds):
                        raise RuntimeError("parent did not update its institution")
                    child_attempted.set()
                    if child_write_kind == "create":
                        self._create_child_relation(
                            child_kind,
                            course=target_course,
                            institution_membership=target_membership,
                        )
                    else:
                        if parent_kind == "course":
                            type(relation).objects.filter(pk=relation.pk).update(
                                course=target_course,
                            )
                        else:
                            type(relation).objects.filter(pk=relation.pk).update(
                                institution_membership=target_membership,
                            )
                    child_write_completed.set()
                    if not allow_child_commit.wait(self.thread_timeout_seconds):
                        raise RuntimeError("test did not release the child transaction")
                outcomes.put(("child", "committed", database_pid))
            except DatabaseError:
                outcomes.put(("child", "rejected", database_pid))
            except Exception as error:
                outcomes.put(("child", "error", database_pid, repr(error)))
            finally:
                close_old_connections()

        parent_thread = threading.Thread(target=parent_update)
        child_thread = threading.Thread(target=child_create)
        parent_thread.start()
        child_thread.start()
        self.assertTrue(
            child_attempted.wait(self.thread_timeout_seconds),
            "child did not attempt its write",
        )
        if child_write_completed.wait(self.thread_timeout_seconds):
            allow_child_commit.set()
        allow_parent_commit.set()
        allow_child_commit.set()
        parent_thread.join(self.thread_timeout_seconds)
        child_thread.join(self.thread_timeout_seconds)

        self.assertFalse(parent_thread.is_alive(), "parent thread did not finish")
        self.assertFalse(child_thread.is_alive(), "child thread did not finish")
        results = {
            outcome[0]: outcome[1:]
            for outcome in [
                outcomes.get(timeout=self.thread_timeout_seconds),
                outcomes.get(timeout=self.thread_timeout_seconds),
            ]
        }
        self.assertEqual(results["parent"][0], "committed", results)
        self.assertEqual(results["child"][0], "rejected", results)
        self.assertNotEqual(results["parent"][1], results["child"][1])
        if parent_kind == "course":
            parent_course.refresh_from_db()
            self.assertEqual(parent_course.institution, self.institution_b)
        else:
            parent_membership.refresh_from_db()
            self.assertEqual(parent_membership.institution, self.institution_b)

        if relation is not None:
            relation.refresh_from_db()
            if source_course is not None:
                self.assertEqual(relation.course, source_course)
            if source_membership is not None:
                self.assertEqual(relation.institution_membership, source_membership)

        self.assertFalse(
            CourseMembership.objects.exclude(
                course__institution_id=F("institution_membership__institution_id"),
            ).exists(),
        )
        self.assertFalse(
            CourseAccessRestriction.objects.exclude(
                course__institution_id=F("institution_membership__institution_id"),
            ).exists(),
        )

    @staticmethod
    def _create_child_relation(child_kind, *, course, institution_membership):
        if child_kind == "membership":
            return CourseMembership.objects.create(
                course=course,
                institution_membership=institution_membership,
                role="owner",
            )
        return CourseAccessRestriction.objects.create(
            course=course,
            institution_membership=institution_membership,
            denied=True,
        )

    def test_parent_update_racing_course_membership_create_cannot_commit_mismatch(self):
        self._assert_parent_update_rejects_racing_child_write(
            parent_kind="course",
            child_kind="membership",
            child_write_kind="create",
        )

    def test_parent_update_racing_restriction_create_cannot_commit_mismatch(self):
        self._assert_parent_update_rejects_racing_child_write(
            parent_kind="course",
            child_kind="restriction",
            child_write_kind="create",
        )

    def test_membership_parent_update_racing_course_membership_create_cannot_commit_mismatch(
        self,
    ):
        self._assert_parent_update_rejects_racing_child_write(
            parent_kind="institution_membership",
            child_kind="membership",
            child_write_kind="create",
        )

    def test_membership_parent_update_racing_restriction_create_cannot_commit_mismatch(
        self,
    ):
        self._assert_parent_update_rejects_racing_child_write(
            parent_kind="institution_membership",
            child_kind="restriction",
            child_write_kind="create",
        )

    def test_course_parent_update_racing_course_membership_reassignment_cannot_commit_mismatch(
        self,
    ):
        self._assert_parent_update_rejects_racing_child_write(
            parent_kind="course",
            child_kind="membership",
            child_write_kind="reassign",
        )

    def test_course_parent_update_racing_restriction_reassignment_cannot_commit_mismatch(
        self,
    ):
        self._assert_parent_update_rejects_racing_child_write(
            parent_kind="course",
            child_kind="restriction",
            child_write_kind="reassign",
        )

    def test_membership_parent_update_racing_course_membership_reassignment_cannot_commit_mismatch(
        self,
    ):
        self._assert_parent_update_rejects_racing_child_write(
            parent_kind="institution_membership",
            child_kind="membership",
            child_write_kind="reassign",
        )

    def test_membership_parent_update_racing_restriction_reassignment_cannot_commit_mismatch(
        self,
    ):
        self._assert_parent_update_rejects_racing_child_write(
            parent_kind="institution_membership",
            child_kind="restriction",
            child_write_kind="reassign",
        )

    def test_nonconflicting_child_create_commits_while_parent_update_is_open(self):
        course_to_update = Course.objects.create(
            institution=self.institution_a,
            course_code="race-parent",
            name="Race Parent",
        )
        independent_course = Course.objects.create(
            institution=self.institution_b,
            course_code="race-independent",
            name="Race Independent",
        )
        parent_updated = threading.Event()
        child_committed = threading.Event()
        outcomes = queue.Queue()

        def parent_update():
            try:
                close_old_connections()
                with transaction.atomic():
                    Course.objects.filter(pk=course_to_update.pk).update(
                        institution=self.institution_b,
                    )
                    parent_updated.set()
                    if not child_committed.wait(self.thread_timeout_seconds):
                        raise RuntimeError("nonconflicting child did not commit")
                outcomes.put(("parent", "committed"))
            except Exception as error:
                outcomes.put(("parent", "error", repr(error)))
            finally:
                close_old_connections()

        def child_create():
            try:
                close_old_connections()
                if not parent_updated.wait(self.thread_timeout_seconds):
                    raise RuntimeError("parent did not update its institution")
                with transaction.atomic():
                    CourseMembership.objects.create(
                        course=independent_course,
                        institution_membership=self.membership_b,
                        role="instructor",
                    )
                child_committed.set()
                outcomes.put(("child", "committed"))
            except Exception as error:
                outcomes.put(("child", "error", repr(error)))
            finally:
                close_old_connections()

        parent_thread = threading.Thread(target=parent_update)
        child_thread = threading.Thread(target=child_create)
        parent_thread.start()
        child_thread.start()
        parent_thread.join(self.thread_timeout_seconds)
        child_thread.join(self.thread_timeout_seconds)

        self.assertFalse(parent_thread.is_alive(), "parent thread did not finish")
        self.assertFalse(child_thread.is_alive(), "child thread did not finish")
        results = {
            outcome[0]: outcome[1:]
            for outcome in [
                outcomes.get(timeout=self.thread_timeout_seconds),
                outcomes.get(timeout=self.thread_timeout_seconds),
            ]
        }
        self.assertEqual(results["parent"][0], "committed")
        self.assertEqual(results["child"][0], "committed")
        self.assertTrue(
            CourseMembership.objects.filter(
                course=independent_course,
                institution_membership=self.membership_b,
            ).exists()
        )
