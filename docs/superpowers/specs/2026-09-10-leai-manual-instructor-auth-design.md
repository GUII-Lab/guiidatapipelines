# LEAI Manual Instructor Authentication Foundation

**Date:** 2026-09-10

**Status:** Approved implementation direction for the controlled first rollout

## Goal

Establish the long-term instructor, institution, and course-ownership relationships without waiting for email delivery. Harvey can provision an instructor account, privately provide a temporary password, and assign legacy courses. The instructor must replace the temporary password before using protected course-management APIs.

## Scope

This slice includes:

- `Institution`, `InstructorAccount`, `InstitutionMembership`, and `CourseMembership` relationships;
- nullable `Course.institution` for additive legacy migration;
- an explicit legacy-course-password compatibility flag;
- opaque, revocable instructor bearer sessions;
- manual account/course provisioning through a Django management command;
- login, logout, current-account, password-change, course-list, and authenticated course-create APIs;
- a future Cognito link point through `auth_provider` and `external_subject`.

This slice excludes:

- self-registration;
- email ownership verification;
- invitation, verification, password-reset, reminder, or digest emails;
- Cognito token acceptance;
- student accounts or rosters;
- broad authorization retrofit of every legacy LEAI endpoint;
- Question Set, response-session, preview, and recurrence models.

## Identity boundary

Django remains authoritative for institution membership, course membership, and authorization. A Django auth user stores the temporary/manual password. `InstructorAccount` stores the stable LEAI identity and a nullable external-provider subject so Cognito can replace the manual password entry point later without rewriting ownership records.

Manual provisioning is an operator approval, not proof that the instructor controls the email address. `email_verified_at` therefore remains null. No API or UI may describe a manually provisioned address as verified.

## Data model

### Institution

- unique lowercase `slug`;
- display `name`;
- active flag and timestamps.

### InstructorAccount

- one-to-one Django auth user;
- unique normalized email and display name;
- `auth_provider`, initially `manual`, with `cognito` reserved;
- nullable unique `external_subject` for the future Cognito `sub`;
- nullable `email_verified_at`;
- `must_change_password`, active flag, and timestamps.

### InstitutionMembership

- one instructor and one institution;
- role `member` or `admin`;
- active flag and timestamps;
- unique instructor/institution pair.

### CourseMembership

- one course and one institution membership;
- role `owner`, `instructor`, or `ta`;
- optional `can_publish` and `can_export` grants for TA exceptions;
- active flag and timestamps;
- unique course/institution-membership pair;
- at most one active owner per course;
- application validation requires the course and membership to belong to the same institution.

### InstructorSession

- stores only a SHA-256 digest of a random bearer token;
- belongs to one instructor account;
- expires after 12 hours by default;
- supports explicit revocation;
- raw tokens are returned once and never persisted or logged.

### LegacyCourseOwnershipReview

- one row per legacy course when reviewed;
- state `linked`, `ambiguous`, or `unresolved`;
- optional linked course membership;
- operator notes and review timestamp;
- no automatic ownership inference from the shared course password.

## Legacy compatibility

The additive migration sets `legacy_password_login_enabled=True` for every existing course, then changes the model default to false. New authenticated course creation stores an unusable value in the old password field and leaves legacy password login disabled. Existing courses continue to work until individually reviewed and cut over; no legacy password or course is deleted in this slice.

## Session and password behavior

- Login accepts normalized email plus password and returns the same generic 401 for unknown account, wrong password, or inactive account.
- A manually provisioned account can log in with its temporary password, but protected course operations return `403 password_change_required` until it changes that password.
- Password change validates the current password and Django's configured password validators, clears `must_change_password`, and revokes every other active session.
- Logout revokes the presented session.
- Session tokens are sent as `Authorization: Bearer <token>` and are kept in browser `sessionStorage`, not URLs or localStorage.

## Provisioning behavior

`python manage.py provision_leai_instructor` creates an account and institution membership atomically, generates a temporary password, and prints it once. Optional repeated `--course-id` arguments attach reviewed legacy courses. A course already attached to another institution or already holding a different active owner causes the entire command to fail without partial writes.

## API boundary

- `POST /datapipeline/api/instructor_sessions/`
- `DELETE /datapipeline/api/instructor_sessions/current/`
- `GET /datapipeline/api/instructor_me/`
- `POST /datapipeline/api/instructor_password/`
- `GET /datapipeline/api/instructor_courses/`
- `POST /datapipeline/api/instructor_courses/`

The authenticated course-create endpoint requires an active institution membership and creates the course plus owner membership in one transaction. It never accepts or creates a shared course password.

## Security and rollout boundary

This is local implementation only. Production remains blocked until migration rehearsal, record-count parity, ownership review, endpoint authorization coverage, rate limiting, production CORS/debug/secret hardening, and live-browser verification pass. No production database migration is authorized by this design.

## Verification

- model constraint and cross-institution validation tests;
- migration test proving old courses keep compatibility while new courses default off;
- login/session/password tests against real database rows;
- command transaction and ownership-conflict tests;
- authenticated course-list/create tests, including password-change gating;
- full backend test suite and migration checks;
- no frontend or production claim until real browser verification is completed in the later frontend slice.
