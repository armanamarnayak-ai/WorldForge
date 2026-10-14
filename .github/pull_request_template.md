## Problem and resulting behavior

Describe the concrete trigger, previous behavior, and resulting behavior. Link relevant issues.

## Implementation and architecture

Describe the working change and decisions needed for review. List implemented capabilities;
do not describe roadmap ideas as shipped features.

## Validation

List exact commands and observed results, including tests, lint/format/types, build, and relevant
clean-install or replay checks. Distinguish local checks from remote CI results.

## Manual verification

Provide commands a reviewer can run from a clean workspace, expected results, and failure cases.

## Known limitations and risks

Describe compatibility, safety, privacy, determinism, and unsupported behavior relevant to the change.

## Next milestone

State the next recommended scoped milestone. Release preparation should not begin later milestones.

## Review checklist

- [ ] Changes are on a development branch, not directly on `main`.
- [ ] Relevant tests and documented examples were run; evidence is above.
- [ ] Documentation and versioned contracts match the implementation.
- [ ] Diff and repository status were reviewed for secrets, private run data, and generated junk.
- [ ] No empty implementations or unsupported feature claims were added.
- [ ] The PR is for review and will not be merged automatically.
