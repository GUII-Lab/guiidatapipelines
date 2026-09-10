import json

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import is_password_usable, make_password
from django.test import Client, TestCase

from datapipeline.models import (
    Course,
    CourseMembership,
    Institution,
    InstitutionMembership,
    InstructorAccount,
)


class InstructorCourseApiTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.institution = Institution.objects.create(
            slug='ucsc',
            name='UC Santa Cruz',
        )
        self.other_institution = Institution.objects.create(
            slug='other',
            name='Other University',
        )
        self.user = get_user_model().objects.create_user(
            username='teacher@example.edu',
            email='teacher@example.edu',
            password='TemporaryPass123!',
        )
        self.account = InstructorAccount.objects.create(
            user=self.user,
            email='teacher@example.edu',
            display_name='Prof. Test',
        )
        self.institution_membership = InstitutionMembership.objects.create(
            institution=self.institution,
            instructor=self.account,
        )

    def post_json(self, path, payload, token=None):
        headers = {}
        if token:
            headers['HTTP_AUTHORIZATION'] = f'Bearer {token}'
        return self.client.post(
            path,
            data=json.dumps(payload),
            content_type='application/json',
            **headers,
        )

    def login(self):
        response = self.post_json('/datapipeline/api/instructor_sessions/', {
            'email': self.account.email,
            'password': 'TemporaryPass123!',
        })
        self.assertEqual(response.status_code, 201)
        return response.json()['token']

    def ready_token(self):
        self.account.must_change_password = False
        self.account.save(update_fields=['must_change_password'])
        return self.login()

    def test_course_list_requires_authentication(self):
        response = self.client.get('/datapipeline/api/instructor_courses/')

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {'error': 'authentication_required'})

    def test_temporary_password_blocks_course_operations(self):
        token = self.login()

        response = self.client.get(
            '/datapipeline/api/instructor_courses/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {'error': 'password_change_required'})

    def test_course_list_returns_only_active_memberships(self):
        visible = Course.objects.create(
            course_id='visible-course',
            course_name='Visible Course',
            instructor_name='Prof. Test',
            password=make_password(None),
            institution=self.institution,
        )
        hidden = Course.objects.create(
            course_id='hidden-course',
            course_name='Hidden Course',
            instructor_name='Prof. Test',
            password=make_password(None),
            institution=self.institution,
        )
        CourseMembership.objects.create(
            course=visible,
            institution_membership=self.institution_membership,
            role=CourseMembership.ROLE_OWNER,
        )
        CourseMembership.objects.create(
            course=hidden,
            institution_membership=self.institution_membership,
            role=CourseMembership.ROLE_INSTRUCTOR,
            is_active=False,
        )
        token = self.ready_token()

        response = self.client.get(
            '/datapipeline/api/instructor_courses/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['course_id'] for item in response.json()['courses']], ['visible-course'])
        self.assertEqual(response.json()['courses'][0]['role'], CourseMembership.ROLE_OWNER)

    def test_course_create_rejects_institution_without_active_membership(self):
        token = self.ready_token()

        response = self.post_json(
            '/datapipeline/api/instructor_courses/',
            {
                'course_id': 'other-course',
                'course_name': 'Other Course',
                'instructor_name': 'Prof. Test',
                'institution_slug': self.other_institution.slug,
            },
            token=token,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {'error': 'institution_access_denied'})
        self.assertFalse(Course.objects.filter(course_id='other-course').exists())

    def test_course_create_atomically_creates_owner_without_shared_password(self):
        token = self.ready_token()

        response = self.post_json(
            '/datapipeline/api/instructor_courses/',
            {
                'course_id': ' New-Course ',
                'course_name': 'New Course',
                'instructor_name': 'Prof. Test',
                'institution_slug': self.institution.slug,
            },
            token=token,
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['course_id'], 'new-course')
        course = Course.objects.get(course_id='new-course')
        owner = CourseMembership.objects.get(course=course)
        self.assertEqual(course.institution, self.institution)
        self.assertFalse(course.legacy_password_login_enabled)
        self.assertFalse(is_password_usable(course.password))
        self.assertEqual(owner.role, CourseMembership.ROLE_OWNER)
        self.assertEqual(owner.institution_membership, self.institution_membership)

    def test_duplicate_course_id_returns_conflict_without_extra_membership(self):
        Course.objects.create(
            course_id='existing',
            course_name='Existing',
            instructor_name='Someone',
            password=make_password(None),
            institution=self.institution,
        )
        token = self.ready_token()

        response = self.post_json(
            '/datapipeline/api/instructor_courses/',
            {
                'course_id': 'existing',
                'course_name': 'Duplicate',
                'instructor_name': 'Prof. Test',
                'institution_slug': self.institution.slug,
            },
            token=token,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {'error': 'course_id_taken'})
        self.assertEqual(Course.objects.filter(course_id='existing').count(), 1)
        self.assertEqual(CourseMembership.objects.count(), 0)

    def test_membership_owned_course_rejects_legacy_password_login(self):
        course = Course.objects.create(
            course_id='membership-course',
            course_name='Membership Course',
            instructor_name='Prof. Test',
            password=make_password('should-not-work'),
            institution=self.institution,
            legacy_password_login_enabled=False,
        )

        response = self.post_json('/datapipeline/api/verify_course_password/', {
            'course_id': course.course_id,
            'password': 'should-not-work',
        })

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {
            'valid': False,
            'error': 'legacy_password_login_disabled',
        })

    def test_compatibility_course_still_accepts_legacy_password(self):
        course = Course.objects.create(
            course_id='legacy-course',
            course_name='Legacy Course',
            instructor_name='Legacy Instructor',
            password=make_password('legacy-password'),
            legacy_password_login_enabled=True,
        )

        response = self.post_json('/datapipeline/api/verify_course_password/', {
            'course_id': course.course_id,
            'password': 'legacy-password',
        })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['valid'])

    def test_public_legacy_course_creation_remains_compatible_until_cutover(self):
        created = self.post_json('/datapipeline/api/create_course/', {
            'course_id': 'legacy-created',
            'course_name': 'Legacy Created',
            'instructor_name': 'Legacy Instructor',
            'password': 'legacy-password',
        })
        self.assertEqual(created.status_code, 200)

        course = Course.objects.get(course_id='legacy-created')
        self.assertTrue(course.legacy_password_login_enabled)
        unlocked = self.post_json('/datapipeline/api/verify_course_password/', {
            'course_id': course.course_id,
            'password': 'legacy-password',
        })
        self.assertEqual(unlocked.status_code, 200)
        self.assertTrue(unlocked.json()['valid'])
