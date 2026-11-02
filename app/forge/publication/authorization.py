"""Phase 9 Git Publication Architecture — Authorization Mapping & Compatibility.

Provides deterministic, fail-closed authorization mapping between Phase 8 Milestone 6
operator approval decisions (AuthorizedAction) and Phase 9 publication actions (PublicationAction).

Guarantees:
- MANUAL_INTEGRATION strictly authorizes EXPORT_PATCH; never STAGE_BRANCH or CREATE_PR.
- EXPORT_PATCH strictly authorizes EXPORT_PATCH; never STAGE_BRANCH or CREATE_PR.
- STAGE_FOR_PR (legacy M6) authorizes STAGE_BRANCH and EXPORT_PATCH; denies CREATE_PR by default.
- STAGE_BRANCH (Phase 9) authorizes STAGE_BRANCH and EXPORT_PATCH; strictly denies CREATE_PR.
- CREATE_PR (Phase 9) authorizes CREATE_PR, STAGE_BRANCH, and EXPORT_PATCH.
- Zero silent privilege escalation across actions, aliases, CLI commands, or database records.
- Complete backward compatibility with existing SQLite approval decision records.
"""

from typing import Dict, Optional, Set
from forge.publication.failures import PublicationActionMismatchError, PublicationAuthorizationError
from forge.publication.models import PublicationAction
from forge.publication.policy import GitPublicationPolicy
from forge.repair.execution.models import AuthorizedAction


# Base mapping: AuthorizedAction -> Set[PublicationAction]
ACTION_AUTHORIZATION_MAP: Dict[AuthorizedAction, Set[PublicationAction]] = {
    # 1. EXPORT_PATCH: Only permits exporting the candidate patch diff / report
    AuthorizedAction.EXPORT_PATCH: {
        PublicationAction.EXPORT_PATCH,
    },
    # 2. MANUAL_INTEGRATION: Only permits exporting the patch for developer review
    AuthorizedAction.MANUAL_INTEGRATION: {
        PublicationAction.EXPORT_PATCH,
    },
    # 3. STAGE_FOR_PR (Legacy M6): Prepares local branch and metadata for PR.
    #    Permits local branch staging (STAGE_BRANCH) and patch export (EXPORT_PATCH).
    #    Strictly FORBIDS remote PR creation (CREATE_PR) by default (fail-closed).
    AuthorizedAction.STAGE_FOR_PR: {
        PublicationAction.EXPORT_PATCH,
        PublicationAction.STAGE_BRANCH,
    },
    # 4. STAGE_BRANCH (Phase 9): Explicit local branch and local commit staging.
    #    Strictly FORBIDS remote PR creation (CREATE_PR).
    AuthorizedAction.STAGE_BRANCH: {
        PublicationAction.EXPORT_PATCH,
        PublicationAction.STAGE_BRANCH,
    },
    # 5. CREATE_PR (Phase 9): Explicit remote PR creation (includes prerequisite local staging).
    AuthorizedAction.CREATE_PR: {
        PublicationAction.EXPORT_PATCH,
        PublicationAction.STAGE_BRANCH,
        PublicationAction.CREATE_PR,
    },
}


def get_permitted_publication_actions(
    authorized_action: AuthorizedAction,
    policy: Optional[GitPublicationPolicy] = None,
) -> Set[PublicationAction]:
    """Return the set of permitted PublicationAction values for a given AuthorizedAction.

    If policy has legacy_stage_for_pr_allows_create_pr enabled, STAGE_FOR_PR will also permit CREATE_PR.
    """
    if authorized_action not in ACTION_AUTHORIZATION_MAP:
        raise PublicationAuthorizationError(
            f"Unrecognized or unsupported authorized action: '{authorized_action}'"
        )

    permitted = set(ACTION_AUTHORIZATION_MAP[authorized_action])

    # Explicit policy opt-in for legacy STAGE_FOR_PR -> CREATE_PR
    if (
        authorized_action == AuthorizedAction.STAGE_FOR_PR
        and policy is not None
        and policy.legacy_stage_for_pr_allows_create_pr
    ):
        permitted.add(PublicationAction.CREATE_PR)

    return permitted


def is_action_authorized(
    authorized_action: AuthorizedAction,
    requested_action: PublicationAction,
    policy: Optional[GitPublicationPolicy] = None,
) -> bool:
    """Check if a requested PublicationAction is permitted under the given AuthorizedAction."""
    permitted = get_permitted_publication_actions(authorized_action, policy)
    return requested_action in permitted


def validate_action_authorization(
    authorized_action: AuthorizedAction,
    requested_action: PublicationAction,
    policy: Optional[GitPublicationPolicy] = None,
) -> None:
    """Validate action compatibility and raise PublicationActionMismatchError with explicit diagnostics."""
    if is_action_authorized(authorized_action, requested_action, policy):
        return

    # Specific diagnostic explanations preventing ambiguous escalation
    if requested_action == PublicationAction.CREATE_PR:
        if authorized_action == AuthorizedAction.MANUAL_INTEGRATION:
            raise PublicationActionMismatchError(
                "Publication action 'create_pr' cannot be authorized by 'manual_integration'. "
                "Manual integration is strictly restricted to patch export for developer review. "
                "Remote pull request creation requires explicit 'create_pr' operator authorization."
            )
        elif authorized_action == AuthorizedAction.EXPORT_PATCH:
            raise PublicationActionMismatchError(
                "Publication action 'create_pr' requires explicit pull request creation authorization; "
                "decision has 'export_patch' (which permits patch export only)."
            )
        elif authorized_action in (AuthorizedAction.STAGE_FOR_PR, AuthorizedAction.STAGE_BRANCH):
            raise PublicationActionMismatchError(
                f"Publication action 'create_pr' requires explicit 'create_pr' operator authorization; "
                f"decision has '{authorized_action.value}' (which authorizes local branch staging only). "
                "Remote publication to origin is refused without explicit 'create_pr' sign-off."
            )

    if requested_action == PublicationAction.STAGE_BRANCH:
        if authorized_action in (AuthorizedAction.EXPORT_PATCH, AuthorizedAction.MANUAL_INTEGRATION):
            raise PublicationActionMismatchError(
                f"Branch staging ('stage_branch') requires explicit 'STAGE_FOR_PR' or 'STAGE_BRANCH' authorization; "
                f"decision has '{authorized_action.value}' (which permits patch export only)."
            )

    permitted = get_permitted_publication_actions(authorized_action, policy)
    raise PublicationActionMismatchError(
        f"Action '{requested_action.value}' not authorized by operator decision ('{authorized_action.value}'). "
        f"Permitted actions under this authorization: {', '.join(sorted(a.value for a in permitted))}."
    )
