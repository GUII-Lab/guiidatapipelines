import json
import uuid

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from datapipeline import models
from datapipeline.question_sets import (
    QuestionSetError,
    complete_preview,
    create_feedback_draft,
    create_survey_from_revision,
    freeze_draft,
    issue_preview_capability,
    list_templates,
    restore_feedback_draft,
    publish_template_to_community,
    save_private_template,
    save_feedback_draft,
    save_preview_message,
    serialize_draft_versions,
    withdraw_template_from_community,
    validate_feedback_body,
)


class FeedbackBuilderFoundationTests(TestCase):
    def setUp(self):
        self.institution = models.Institution.objects.create(
            slug='ucsc-v12',
            name='University of California, Santa Cruz',
        )
        user = get_user_model().objects.create_user(
            username='v12-teacher@ucsc.edu',
            email='v12-teacher@ucsc.edu',
            password='TemporaryPass123!',
        )
        self.account = models.InstructorAccount.objects.create(
            user=user,
            email='v12-teacher@ucsc.edu',
            display_name='V12 Teacher',
            must_change_password=False,
        )
        self.course = models.Course.objects.create(
            course_id='v12-foundation',
            course_name='V12 Foundation',
            instructor_name='V12 Teacher',
            password=make_password(None),
            institution=self.institution,
        )

    def require_foundation_models(self):
        names = (
            'QuestionSetTemplate',
            'QuestionSetTemplateRevision',
            'QuestionSetDraftVersion',
        )
        missing = [name for name in names if not hasattr(models, name)]
        self.assertEqual(missing, [], f'missing v12 foundation models: {missing}')
        return tuple(getattr(models, name) for name in names)

    def guided_body(self, title='Weekly Reflection'):
        return {
            'schema_version': 'guided-feedback-v2',
            'title': title,
            'intro': 'Reflect on this week.',
            'sections': [{
                'id': '11111111-1111-4111-8111-111111111111',
                'title': 'Learning and practice',
                'questions': [{
                    'id': '22222222-2222-4222-8222-222222222222',
                    'short_label': 'Key concepts',
                    'prompt': 'What stayed with you this week, and why?',
                    'follow_up': {
                        'enabled': True,
                        'prompt': 'What helped it click?',
                    },
                    'response_kind': 'long_text',
                }],
            }],
            'closing': {'prompt': 'Anything else your instructor should know?'},
        }

    def test_foundation_models_exist(self):
        self.require_foundation_models()

    def test_magy_weekly_template_is_seeded_with_stable_uuid_ids(self):
        QuestionSetTemplate, _, _ = self.require_foundation_models()
        template = QuestionSetTemplate.objects.get(
            name='Weekly Reflection',
            scope='global',
            is_active=True,
        )
        self.assertEqual(template.visibility, 'community')
        revision = template.community_revision
        self.assertIsNotNone(revision)
        self.assertEqual(revision.protocol_schema_version, 'guided-feedback-v2')
        self.assertEqual(len(revision.canonical_body['sections']), 6)
        for section in revision.canonical_body['sections']:
            self.assertEqual(len(section['questions']), 1)
            # Parsing asserts that seeded IDs remain valid stable UUID strings.
            __import__('uuid').UUID(section['id'])
            __import__('uuid').UUID(section['questions'][0]['id'])

    def test_team_template_is_seeded_without_a_team_size_rule(self):
        QuestionSetTemplate, _, _ = self.require_foundation_models()
        template = QuestionSetTemplate.objects.get(
            name='Team Collaboration Check-in',
            audience='team',
            collection_style='guided',
        )
        self.assertEqual(template.visibility, 'community')
        self.assertIsNotNone(template.community_revision)
        body = template.community_revision.canonical_body
        self.assertEqual(body['schema_version'], 'guided-feedback-v2')
        self.assertNotIn('minimum_team_size', body)

    def test_question_set_accepts_only_the_three_supported_audience_styles(self):
        self.require_foundation_models()
        supported = (
            ('individual', 'guided', 'course'),
            ('individual', 'open', 'course'),
            ('team', 'guided', 'team'),
        )
        for index, (audience, collection_style, aggregation_scope) in enumerate(supported):
            question_set = models.QuestionSet(
                course=self.course,
                owner=self.account,
                template_id='',
                title=f'Supported {index}',
                audience=audience,
                collection_style=collection_style,
                source_kind='blank',
                response_unit='individual',
                aggregation_scope=aggregation_scope,
            )
            question_set.full_clean()

        unsupported = models.QuestionSet(
            course=self.course,
            owner=self.account,
            template_id='',
            title='Unsupported Team Open',
            audience='team',
            collection_style='open',
            source_kind='blank',
            response_unit='individual',
            aggregation_scope='team',
        )
        with self.assertRaises(ValidationError):
            unsupported.full_clean()

    def test_instructor_template_is_private_and_pins_direct_and_root_lineage(self):
        QuestionSetTemplate, QuestionSetTemplateRevision, _ = self.require_foundation_models()
        root = QuestionSetTemplate.objects.create(
            name='Root template',
            description='Root',
            scope='instructor',
            owner=self.account,
            institution=self.institution,
            audience='individual',
            collection_style='guided',
        )
        self.assertEqual(root.visibility, 'private')
        root_revision = QuestionSetTemplateRevision.objects.create(
            template=root,
            revision_number=1,
            canonical_body=self.guided_body('Root template'),
            content_hash='a' * 64,
            protocol_schema_version='guided-feedback-v2',
            created_by=self.account,
            provenance='instructor_saved',
        )
        root.origin_revision = root_revision
        root.save(update_fields=['origin_revision', 'updated_at'])

        fork = QuestionSetTemplate.objects.create(
            name='Fork',
            description='Fork',
            scope='instructor',
            owner=self.account,
            institution=self.institution,
            audience='individual',
            collection_style='guided',
            forked_from_revision=root_revision,
            origin_revision=root_revision,
        )
        self.assertEqual(fork.forked_from_revision, root_revision)
        self.assertEqual(fork.origin_revision, root_revision)

    def test_template_revision_is_immutable(self):
        QuestionSetTemplate, QuestionSetTemplateRevision, _ = self.require_foundation_models()
        template = QuestionSetTemplate.objects.create(
            name='Private template',
            description='',
            scope='instructor',
            owner=self.account,
            institution=self.institution,
            audience='individual',
            collection_style='guided',
        )
        revision = QuestionSetTemplateRevision.objects.create(
            template=template,
            revision_number=1,
            canonical_body=self.guided_body(),
            content_hash='b' * 64,
            protocol_schema_version='guided-feedback-v2',
            created_by=self.account,
            provenance='instructor_saved',
        )
        revision.canonical_body = self.guided_body('Changed')
        with self.assertRaises(ValidationError):
            revision.save()

    def test_draft_version_keeps_recoverable_full_body_and_parent(self):
        _, _, QuestionSetDraftVersion = self.require_foundation_models()
        question_set = models.QuestionSet.objects.create(
            course=self.course,
            owner=self.account,
            template_id='',
            title='Recoverable',
            audience='individual',
            collection_style='guided',
            source_kind='blank',
            response_unit='individual',
            aggregation_scope='course',
        )
        first = QuestionSetDraftVersion.objects.create(
            question_set=question_set,
            version_number=1,
            canonical_body=self.guided_body(),
            content_hash='c' * 64,
            author_kind='instructor',
            author=self.account,
            change_set=[],
            summary='Starting point',
            rationale='',
        )
        second = QuestionSetDraftVersion.objects.create(
            question_set=question_set,
            version_number=2,
            parent_version=first,
            canonical_body=self.guided_body('Edited'),
            content_hash='d' * 64,
            author_kind='ai',
            author=self.account,
            change_set=[{'op': 'set_title', 'value': 'Edited'}],
            summary='Changed title',
            rationale='Matched the instructor request.',
        )
        self.assertEqual(second.parent_version, first)
        self.assertEqual(json.loads(json.dumps(second.canonical_body))['title'], 'Edited')

        restored = QuestionSetDraftVersion.objects.create(
            question_set=question_set,
            version_number=3,
            parent_version=second,
            restored_from=first,
            canonical_body=first.canonical_body,
            content_hash=first.content_hash,
            author_kind='restore',
            author=self.account,
            change_set=[],
            summary='Restored Version 1',
            rationale='',
        )
        self.assertEqual(restored.restored_from, first)
        self.assertEqual(restored.content_hash, first.content_hash)


class FeedbackBuilderValidationTests(FeedbackBuilderFoundationTests):
    def open_body(self):
        return {
            'schema_version': 'open-conversation-v1',
            'title': 'Course experience check-in',
            'opening_prompt': 'How is your learning experience going right now?',
            'listening_goal': 'Learning supports, blockers, workload, and suggestions.',
            'closing_prompt': 'Is there anything else your instructor should know?',
        }

    def test_guided_validator_generates_missing_ids_and_preserves_existing_ids(self):
        body = self.guided_body()
        preserved = body['sections'][0]['id']
        del body['sections'][0]['questions'][0]['id']
        normalized, result = validate_feedback_body(
            body,
            audience='individual',
            collection_style='guided',
        )
        self.assertEqual(normalized['sections'][0]['id'], preserved)
        uuid.UUID(normalized['sections'][0]['questions'][0]['id'])
        self.assertEqual(result['question_count'], 1)

    def test_guided_limits_response_kind_and_team_taxonomy(self):
        body = self.guided_body()
        body['sections'][0]['questions'][0]['response_kind'] = 'multiple_choice'
        with self.assertRaises(QuestionSetError):
            validate_feedback_body(
                body,
                audience='individual',
                collection_style='guided',
            )
        validate_feedback_body(
            self.guided_body(),
            audience='team',
            collection_style='guided',
        )
        with self.assertRaises(QuestionSetError):
            validate_feedback_body(
                self.open_body(),
                audience='team',
                collection_style='open',
            )

    def test_guided_validator_enforces_section_question_and_size_limits(self):
        too_many_sections = self.guided_body()
        too_many_sections['sections'] = [
            {
                'id': str(uuid.uuid4()),
                'title': f'Section {index}',
                'questions': [{
                    'id': str(uuid.uuid4()),
                    'short_label': 'Question',
                    'prompt': 'What happened?',
                    'follow_up': {'enabled': False, 'prompt': ''},
                    'response_kind': 'long_text',
                }],
            }
            for index in range(13)
        ]
        with self.assertRaises(QuestionSetError):
            validate_feedback_body(
                too_many_sections,
                audience='individual',
                collection_style='guided',
            )

        too_many_questions = self.guided_body()
        too_many_questions['sections'][0]['questions'] = [
            {
                'id': str(uuid.uuid4()),
                'short_label': f'Question {index}',
                'prompt': 'What happened?',
                'follow_up': {'enabled': False, 'prompt': ''},
                'response_kind': 'long_text',
            }
            for index in range(25)
        ]
        with self.assertRaises(QuestionSetError):
            validate_feedback_body(
                too_many_questions,
                audience='individual',
                collection_style='guided',
            )

        oversized = self.open_body()
        oversized['listening_goal'] = 'x' * (64 * 1024)
        with self.assertRaises(QuestionSetError):
            validate_feedback_body(
                oversized,
                audience='individual',
                collection_style='open',
            )

    def test_open_validator_accepts_the_bounded_open_contract(self):
        normalized, result = validate_feedback_body(
            self.open_body(),
            audience='individual',
            collection_style='open',
        )
        self.assertEqual(normalized['schema_version'], 'open-conversation-v1')
        self.assertEqual(result['question_count'], 1)

    def test_templates_are_authorized_filtered_and_pin_an_exact_revision(self):
        templates = list_templates(
            actor=self.account,
            audience='individual',
            collection_style='guided',
        )
        weekly = next(item for item in templates if item['name'] == 'Weekly Reflection')
        self.assertEqual(weekly['source'], 'leai')
        self.assertTrue(weekly['revision_id'])

        draft = create_feedback_draft(
            course=self.course,
            actor=self.account,
            instructor_session=None,
            audience='individual',
            collection_style='guided',
            source_kind='template',
            source_template_revision_id=weekly['revision_id'],
        )
        self.assertEqual(
            str(draft.question_set.source_template_revision.public_id),
            weekly['revision_id'],
        )
        self.assertEqual(draft.body, draft.question_set.source_template_revision.canonical_body)

    def test_blank_start_and_confirmed_start_new_replace_one_active_workflow(self):
        first = create_feedback_draft(
            course=self.course,
            actor=self.account,
            instructor_session=None,
            audience='individual',
            collection_style='open',
            source_kind='blank',
        )
        self.assertEqual(first.body['schema_version'], 'open-conversation-v1')
        with self.assertRaises(QuestionSetError) as context:
            create_feedback_draft(
                course=self.course,
                actor=self.account,
                instructor_session=None,
                audience='team',
                collection_style='guided',
                source_kind='blank',
            )
        self.assertEqual(context.exception.code, 'active_draft_exists')

        second = create_feedback_draft(
            course=self.course,
            actor=self.account,
            instructor_session=None,
            audience='team',
            collection_style='guided',
            source_kind='blank',
            confirm_abandon_active=True,
        )
        first.question_set.refresh_from_db()
        self.assertEqual(first.question_set.workflow_status, 'abandoned')
        self.assertEqual(second.question_set.workflow_status, 'active')
        self.assertEqual(second.question_set.aggregation_scope, 'team')


class FeedbackBuilderHistoryTests(FeedbackBuilderValidationTests):
    def create_guided_draft(self):
        return create_feedback_draft(
            course=self.course,
            actor=self.account,
            instructor_session=None,
            audience='individual',
            collection_style='guided',
            source_kind='blank',
        )

    def test_quiet_autosave_does_not_spam_history_and_retry_is_idempotent(self):
        draft = self.create_guided_draft()
        body = json.loads(json.dumps(draft.body))
        body['title'] = 'Autosaved title'
        saved = save_feedback_draft(
            draft_id=draft.public_id,
            actor=self.account,
            instructor_session=None,
            expected_version=1,
            body=body,
            idempotency_key='autosave-version-1',
        )
        self.assertEqual(saved.version, 2)
        self.assertEqual(saved.question_set.draft_versions.count(), 1)

        retried = save_feedback_draft(
            draft_id=draft.public_id,
            actor=self.account,
            instructor_session=None,
            expected_version=1,
            body=body,
            idempotency_key='autosave-version-1',
        )
        self.assertEqual(retried.version, 2)
        self.assertEqual(retried.question_set.draft_versions.count(), 1)

        conflicting = json.loads(json.dumps(body))
        conflicting['title'] = 'Different request'
        with self.assertRaises(QuestionSetError) as context:
            save_feedback_draft(
                draft_id=draft.public_id,
                actor=self.account,
                instructor_session=None,
                expected_version=1,
                body=conflicting,
                idempotency_key='autosave-version-1',
            )
        self.assertEqual(context.exception.code, 'idempotency_key_conflict')

    def test_forced_boundary_checkpoint_history_and_restore_are_append_only(self):
        draft = self.create_guided_draft()
        original = draft.current_checkpoint
        body = json.loads(json.dumps(draft.body))
        body['title'] = 'Ready to preview'
        saved = save_feedback_draft(
            draft_id=draft.public_id,
            actor=self.account,
            instructor_session=None,
            expected_version=1,
            body=body,
            idempotency_key='preview-checkpoint-1',
            checkpoint_reason='preview',
        )
        self.assertEqual(saved.question_set.draft_versions.count(), 2)
        self.assertEqual(saved.current_checkpoint.author_kind, 'instructor')
        rows = serialize_draft_versions(saved.question_set)
        self.assertEqual([row['version_number'] for row in rows], [2, 1])
        self.assertNotIn('canonical_body', rows[0])

        restored = restore_feedback_draft(
            draft_id=draft.public_id,
            actor=self.account,
            instructor_session=None,
            expected_version=2,
            version_id=original.public_id,
            idempotency_key='restore-version-1',
        )
        self.assertEqual(restored.version, 3)
        self.assertEqual(restored.body['title'], original.canonical_body['title'])
        self.assertEqual(restored.current_checkpoint.author_kind, 'restore')
        self.assertEqual(restored.current_checkpoint.restored_from, original)
        self.assertEqual(restored.question_set.draft_versions.count(), 3)

    def test_stale_autosave_never_overwrites_newer_work(self):
        draft = self.create_guided_draft()
        body = json.loads(json.dumps(draft.body))
        body['title'] = 'First edit'
        save_feedback_draft(
            draft_id=draft.public_id,
            actor=self.account,
            instructor_session=None,
            expected_version=1,
            body=body,
            idempotency_key='first-edit-key',
        )
        with self.assertRaises(QuestionSetError) as context:
            save_feedback_draft(
                draft_id=draft.public_id,
                actor=self.account,
                instructor_session=None,
                expected_version=1,
                body=body,
                idempotency_key='stale-edit-key',
            )
        self.assertEqual(context.exception.code, 'stale_draft')


class FeedbackBuilderPublicationTests(FeedbackBuilderValidationTests):
    def create_publishable(self, *, audience, collection_style, suffix):
        course = models.Course.objects.create(
            course_id=f'publish-{suffix}',
            course_name=f'Publish {suffix}',
            instructor_name='V12 Teacher',
            password=make_password(None),
            institution=self.institution,
        )
        draft = create_feedback_draft(
            course=course,
            actor=self.account,
            instructor_session=None,
            audience=audience,
            collection_style=collection_style,
            source_kind='blank',
        )
        revision, _created = freeze_draft(
            draft_id=draft.public_id,
            actor=self.account,
            instructor_session=None,
            expected_version=draft.version,
        )
        token, preview = issue_preview_capability(
            revision=revision,
            actor=self.account,
            instructor_session=None,
        )
        models.PreviewSession.objects.filter(pk=preview.pk).update(
            ready_at=timezone.now(),
            skipped_at=timezone.now(),
        )
        return course, revision, token

    def publish(self, *, revision, token, key, team_configuration=None):
        return create_survey_from_revision(
            revision=revision,
            actor=self.account,
            instructor_session=None,
            idempotency_key=key,
            survey_label=revision.question_set.title,
            week_number=None,
            opens_at=None,
            expires_at=None,
            preview_token=token,
            team_configuration=team_configuration,
        )

    def test_all_three_feedback_types_publish_to_existing_student_modes(self):
        _course, guided_revision, guided_token = self.create_publishable(
            audience='individual',
            collection_style='guided',
            suffix='guided',
        )
        guided_link, _ = self.publish(
            revision=guided_revision,
            token=guided_token,
            key='publish-guided-v12',
        )
        self.assertEqual(guided_link.survey.mode, 'form')
        self.assertIsNone(guided_link.survey.opens_at)
        self.assertIsNone(guided_link.survey.expires_at)

        _course, open_revision, open_token = self.create_publishable(
            audience='individual',
            collection_style='open',
            suffix='open',
        )
        open_link, _ = self.publish(
            revision=open_revision,
            token=open_token,
            key='publish-open-v12',
        )
        self.assertEqual(open_link.survey.mode, 'general')

        team_course, team_revision, team_token = self.create_publishable(
            audience='team',
            collection_style='guided',
            suffix='team',
        )
        configuration = models.TeamConfiguration.objects.create(
            course=team_course,
            name='Lab teams',
            label_prefix='Team',
        )
        models.Team.objects.create(
            team_configuration=configuration,
            number=1,
            size=2,
            display_name='Pair One',
        )
        team_link, _ = self.publish(
            revision=team_revision,
            token=team_token,
            key='publish-team-v12',
            team_configuration=configuration,
        )
        self.assertEqual(team_link.survey.mode, 'group')
        self.assertEqual(team_link.team_configuration, configuration)
        snapshot = team_link.survey.team_snapshot
        self.assertEqual(snapshot.teams.get(number=1).size, 2)
        self.assertEqual(team_link.publication_manifest['team_selection'], 'self_selected')

    def test_open_preview_completes_only_after_its_closing_exchange(self):
        _course, revision, token = self.create_publishable(
            audience='individual',
            collection_style='open',
            suffix='open-preview',
        )
        models.PreviewSession.objects.filter(revision=revision).update(skipped_at=None)
        save_preview_message(
            raw_token=token,
            role='assistant',
            content='What would you like your instructor to understand?',
        )
        save_preview_message(raw_token=token, role='user', content='The pace is fast.')
        with self.assertRaises(QuestionSetError) as context:
            complete_preview(raw_token=token)
        self.assertEqual(context.exception.code, 'preview_incomplete')

        closing = revision.compiled_protocol['closing_prompt']
        save_preview_message(raw_token=token, role='assistant', content=closing)
        save_preview_message(raw_token=token, role='user', content='That is all.')
        save_preview_message(raw_token=token, role='assistant', content='Thank you.')
        preview = complete_preview(raw_token=token)
        self.assertIsNotNone(preview.completed_at)

    def test_team_publication_requires_same_course_configuration_but_no_size_threshold(self):
        team_course, revision, token = self.create_publishable(
            audience='team',
            collection_style='guided',
            suffix='team-validation',
        )
        with self.assertRaises(QuestionSetError) as context:
            self.publish(
                revision=revision,
                token=token,
                key='publish-team-no-config',
            )
        self.assertEqual(context.exception.code, 'team_configuration_required')

        configuration = models.TeamConfiguration.objects.create(
            course=team_course,
            name='Two-person teams',
        )
        models.Team.objects.create(
            team_configuration=configuration,
            number=1,
            size=2,
        )
        link, created = self.publish(
            revision=revision,
            token=token,
            key='publish-team-two-people',
            team_configuration=configuration,
        )
        self.assertTrue(created)
        self.assertEqual(link.survey.team_snapshot.teams.count(), 1)

    def test_private_template_community_revision_withdrawal_and_lineage(self):
        _course, revision, _token = self.create_publishable(
            audience='individual',
            collection_style='guided',
            suffix='template-source',
        )
        template, template_revision = save_private_template(
            revision=revision,
            actor=self.account,
            name='My guided feedback',
            description='Reusable questions',
        )
        self.assertEqual(template.visibility, 'private')
        self.assertEqual(template.origin_revision, template_revision)

        publish_template_to_community(
            template=template,
            revision=template_revision,
            actor=self.account,
        )
        template.refresh_from_db()
        self.assertEqual(template.community_revision, template_revision)

        other_user = get_user_model().objects.create_user(
            username='other-v12@ucsc.edu',
            email='other-v12@ucsc.edu',
            password='TemporaryPass123!',
        )
        other = models.InstructorAccount.objects.create(
            user=other_user,
            email='other-v12@ucsc.edu',
            display_name='Other Teacher',
            must_change_password=False,
        )
        discovered = list_templates(
            actor=other,
            audience='individual',
            collection_style='guided',
        )
        self.assertIn(str(template_revision.public_id), {
            item['revision_id'] for item in discovered
        })

        fork_course = models.Course.objects.create(
            course_id='community-fork-course',
            course_name='Community fork course',
            instructor_name='Other Teacher',
            password=make_password(None),
            institution=self.institution,
        )
        fork_draft = create_feedback_draft(
            course=fork_course,
            actor=other,
            instructor_session=None,
            audience='individual',
            collection_style='guided',
            source_kind='template',
            source_template_revision_id=str(template_revision.public_id),
        )
        fork_revision, _ = freeze_draft(
            draft_id=fork_draft.public_id,
            actor=other,
            instructor_session=None,
            expected_version=fork_draft.version,
        )
        fork_template, _fork_template_revision = save_private_template(
            revision=fork_revision,
            actor=other,
            name='Forked guided feedback',
        )
        self.assertEqual(fork_template.forked_from_revision, template_revision)
        self.assertEqual(fork_template.origin_revision, template_revision)

        withdraw_template_from_community(template=template, actor=self.account)
        after_withdrawal = list_templates(
            actor=other,
            audience='individual',
            collection_style='guided',
        )
        self.assertNotIn(str(template_revision.public_id), {
            item['revision_id'] for item in after_withdrawal
        })
        self.assertTrue(
            models.QuestionSetTemplateRevision.objects.filter(pk=template_revision.pk).exists()
        )
