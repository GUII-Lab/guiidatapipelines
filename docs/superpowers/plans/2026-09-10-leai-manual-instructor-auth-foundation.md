# LEAI Manual Instructor Authentication Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add manually provisioned instructor identities, institution/course ownership, secure temporary-password sessions, and authenticated course creation without email verification.

**Architecture:** Django owns all LEAI authorization relationships. A narrow bearer-session adapter authenticates manually provisioned Django passwords now and leaves `InstructorAccount.external_subject` available for Cognito later. Legacy courses remain readable through an explicit compatibility flag while new courses are membership-owned and have no usable shared password.

**Tech Stack:** Django 4.2, PostgreSQL, Django auth password hashing/validation, Django TestCase/Client.

**Spec:** `docs/superpowers/specs/2026-09-10-leai-manual-instructor-auth-design.md`

**Execution status:** Backend foundation implemented and locally verified on 2026-09-10. Production migration, endpoint cutover, frontend wiring, and deployment remain gated.

## Global Constraints

- Do not add self-registration, email verification, password-reset email, reminders, or Cognito token handling.
- Never reuse the Parallel Cognito user pool.
- Preserve all legacy Course rows and passwords; migrations are additive and cleanup is separately approved.
- Keep students anonymous and session-based.
- Store only SHA-256 session-token digests; never log or persist raw tokens.
- Do not deploy or run migrations against production.

---

### Task 1: Add identity, membership, compatibility, and session models

**Files:**
- Modify: `datapipeline/models.py`
- Create: `datapipeline/migrations/0045_instructor_identity_foundation.py`
- Create: `datapipeline/tests/test_instructor_models.py`

**Interfaces:**
- Produces: `Institution`, `InstructorAccount`, `InstitutionMembership`, `CourseMembership`, `InstructorSession`, and `LegacyCourseOwnershipReview`.
- Produces: nullable `Course.institution` and `Course.legacy_password_login_enabled` with existing-row true/new-row false behavior.

- [x] **Step 1: Write failing model tests**

Add tests that create two institutions and prove a `CourseMembership` rejects an institution membership from another institution, that a second active owner violates the unique owner constraint, and that new `Course` instances default `legacy_password_login_enabled` to false.

```python
def test_course_membership_rejects_cross_institution_membership(self):
    self.course.institution = self.ucsc
    self.course.save(update_fields=["institution"])
    membership = CourseMembership(
        course=self.course,
        institution_membership=self.other_membership,
        role=CourseMembership.ROLE_INSTRUCTOR,
    )
    with self.assertRaises(ValidationError):
        membership.full_clean()

def test_new_course_disables_legacy_password_login(self):
    course = Course.objects.create(
        course_id="new-course",
        course_name="New Course",
        instructor_name="Instructor",
        password=make_password(None),
    )
    self.assertFalse(course.legacy_password_login_enabled)
```

- [x] **Step 2: Run tests and verify RED**

Run:

```bash
python manage.py test datapipeline.tests.test_instructor_models --verbosity 2
```

Expected: import/model failures because the new models and fields do not exist.

- [x] **Step 3: Implement models and hand-written additive migration**

Add model constants and constraints from the spec. `CourseMembership.clean()` must raise `ValidationError` when `course.institution_id != institution_membership.institution_id`. Its `save()` calls `full_clean()` before persistence. The migration adds `legacy_password_login_enabled` with existing-row value true, then alters the model default to false.

- [x] **Step 4: Verify GREEN and migration consistency**

Run:

```bash
python manage.py test datapipeline.tests.test_instructor_models --verbosity 2
python manage.py makemigrations --check --dry-run
python manage.py migrate --plan
```

Expected: tests pass, no model drift, migration plan ends at `0045_instructor_identity_foundation`.

---

### Task 2: Add opaque instructor-session authentication

**Files:**
- Create: `datapipeline/instructor_auth.py`
- Create: `datapipeline/instructor_views.py`
- Modify: `datapipeline/urls.py`
- Create: `datapipeline/tests/test_instructor_sessions.py`

**Interfaces:**
- Produces: `issue_instructor_session(account) -> (raw_token, InstructorSession)`.
- Produces: `authenticate_instructor_request(request) -> InstructorAccount | None`.
- Produces endpoints `instructor_sessions`, `instructor_sessions/current`, `instructor_me`, and `instructor_password`.

- [x] **Step 1: Write failing login and token-storage tests**

Test a successful login, a generic 401 for both unknown email and wrong password, and prove the raw returned token differs from the persisted 64-character digest.

```python
response = self.post_json("/datapipeline/api/instructor_sessions/", {
    "email": "teacher@example.edu",
    "password": "TemporaryPass123!",
})
self.assertEqual(response.status_code, 201)
raw_token = response.json()["token"]
stored = InstructorSession.objects.get()
self.assertNotEqual(raw_token, stored.token_digest)
self.assertEqual(hashlib.sha256(raw_token.encode()).hexdigest(), stored.token_digest)
```

- [x] **Step 2: Run session tests and verify RED**

Run:

```bash
python manage.py test datapipeline.tests.test_instructor_sessions --verbosity 2
```

Expected: endpoint/module import failures.

- [x] **Step 3: Implement token issuance and request authentication**

Generate tokens with `secrets.token_urlsafe(32)`, persist `sha256(raw.encode()).hexdigest()`, and expire them after `LEAI_INSTRUCTOR_SESSION_HOURS` with a default of 12. Authentication accepts only one `Bearer` token and rejects missing, malformed, expired, revoked, or inactive-account sessions.

- [x] **Step 4: Implement login, logout, and current-account endpoints**

Normalize email with `strip().lower()`. Login uses the linked Django user's `check_password`; every authentication failure returns:

```json
{"error":"invalid_credentials"}
```

with status 401. `instructor_me` returns account identity, `must_change_password`, and active institution/course memberships without exposing password or token digests.

- [x] **Step 5: Write failing password-change tests**

Cover wrong current password, weak new password, successful password change, `must_change_password` clearing, and revocation of all other sessions.

- [x] **Step 6: Implement password change and verify GREEN**

Use `django.contrib.auth.password_validation.validate_password`. Keep the presented session active and revoke other active sessions in one transaction.

Run:

```bash
python manage.py test datapipeline.tests.test_instructor_sessions --verbosity 2
```

Expected: all session tests pass.

---

### Task 3: Add atomic manual provisioning

**Files:**
- Create: `datapipeline/management/__init__.py`
- Create: `datapipeline/management/commands/__init__.py`
- Create: `datapipeline/management/commands/provision_leai_instructor.py`
- Create: `datapipeline/tests/test_instructor_provisioning.py`

**Interfaces:**
- Produces command `provision_leai_instructor --email [--display-name ...] [--course-id ...]`; the first UCSC rollout defaults institution slug/name and derives an omitted display name from the email prefix.

- [x] **Step 1: Write failing command tests**

Use `call_command` to prove the command creates one auth user, instructor account, institution membership, and optional owner course membership. Assert `must_change_password=True`, `email_verified_at is None`, the legacy course receives the selected institution, and output includes a non-empty temporary password.

- [x] **Step 2: Add atomic conflict tests**

Pre-attach one requested course to a different institution and assert the command raises `CommandError` while creating no new account, user, or partial membership.

- [x] **Step 3: Run provisioning tests and verify RED**

Run:

```bash
python manage.py test datapipeline.tests.test_instructor_provisioning --verbosity 2
```

Expected: command lookup failure.

- [x] **Step 4: Implement the command**

Normalize and validate the exact `ucsc.edu` email domain, derive an omitted display name from the email prefix, create a random 20-character temporary password, create the Django user with `create_user`, and perform every database write inside one `transaction.atomic()`. Never auto-link an existing account by matching an unverified email. Existing-account or ownership conflicts fail explicitly.

- [x] **Step 5: Verify GREEN**

Run the focused provisioning tests and confirm all rows roll back on conflict.

---

### Task 4: Add authenticated course list and creation

**Files:**
- Modify: `datapipeline/instructor_views.py`
- Modify: `datapipeline/urls.py`
- Create: `datapipeline/tests/test_instructor_courses.py`
- Modify: `datapipeline/views.py`

**Interfaces:**
- Produces `GET/POST /datapipeline/api/instructor_courses/`.
- Consumes bearer authentication from Task 2 and ownership models from Task 1.

- [x] **Step 1: Write failing authorization tests**

Cover missing token 401, temporary-password account 403 `password_change_required`, listing only active memberships, and refusing institution slugs where the account has no active membership.

- [x] **Step 2: Write failing atomic creation tests**

Assert POST creates exactly one course and one owner membership, stores an unusable legacy password, disables legacy password login, normalizes the course ID, and returns 409 without partial records for a duplicate ID.

- [x] **Step 3: Run course tests and verify RED**

Run:

```bash
python manage.py test datapipeline.tests.test_instructor_courses --verbosity 2
```

Expected: 404 because the route is absent.

- [x] **Step 4: Implement list/create endpoint**

Require `course_id`, `course_name`, `instructor_name`, and `institution_slug`. Validate `course_id` with Django's slug rules and lowercase it. In one transaction create `Course(password=make_password(None), legacy_password_login_enabled=False)` and its owner membership.

- [x] **Step 5: Guard the legacy password endpoint**

Add a test proving `verify_course_password` returns 403 `legacy_password_login_disabled` for a new membership-owned course while an explicitly compatibility-enabled legacy course still accepts its existing password. Implement the guard before calling `check_password`.

- [x] **Step 6: Verify GREEN**

Run focused course/session/model tests.

---

### Task 5: Backend integrity gate

**Files:**
- Modify: `docs/superpowers/plans/2026-09-10-leai-manual-instructor-auth-foundation.md` to check completed steps.

**Interfaces:**
- Verifies the complete backend foundation; produces no new runtime interface.

- [x] **Step 1: Run migration/model checks**

```bash
python manage.py makemigrations --check --dry-run
python manage.py migrate --plan
python manage.py check --deploy
```

Record expected pre-existing deployment warnings separately; any new error blocks completion.

- [x] **Step 2: Run the full backend suite**

```bash
python manage.py test datapipeline.tests --verbosity 1
```

Expected baseline plus new tests all pass against the isolated test database.

- [x] **Step 3: Inspect migration SQL and repository diff**

```bash
python manage.py sqlmigrate datapipeline 0045
git diff --check
git status --short
```

Confirm the migration contains no destructive drop/rename operations and only task-owned files changed.

- [x] **Step 4: Stop at the release boundary**

Do not deploy, touch production data, add email behavior, or claim the Wizard complete. Hand off the verified backend foundation and then create the separate frontend login/course-selection implementation plan.
