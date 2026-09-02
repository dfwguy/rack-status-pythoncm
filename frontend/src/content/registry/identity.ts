/* SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: LicenseRef-NvidiaProprietary
 *
 * NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
 * property and proprietary rights in and to this material, related
 * documentation and any modifications thereto. Any use, reproduction,
 * disclosure or distribution of this material and related documentation
 * without an express license agreement from NVIDIA CORPORATION or
 * its affiliates is strictly prohibited.
 * Author: Rafael M. Koike & Cursor Agent ("Ada")
 */

/**
 * Knowledge-registry entries for the Identity domain — user and group administration.
 *
 * These feed the contextual help panel and the AI context, so the wording carries the constraints
 * an operator will otherwise discover by hitting a refusal: a login is permanent and never reused,
 * a one-time password is shown exactly once, and groups grant nothing until the policy engine
 * lands.
 */

import type { HelpContent } from "../types";

export const identityContent: HelpContent[] = [
  {
    id: "identityUsers",
    kind: "view",
    title: "Users",
    domain: "Identity",
    routePath: "/identity/users",
    summary: "Who can sign in; create accounts, reset passwords, disable and delete.",
    description:
      "Users manages the local identity store. Accounts authenticate with SRP, so the server holds a verifier and never a password — which shapes everything on this page. A new user receives a one-time password shown exactly once and must choose their own at first sign-in; a reset issues a fresh one and signs the previous holder out everywhere. A login is the principal in every audit record: it cannot be renamed, and once deleted it is retired permanently and can never be created again, because reusing it would reattribute a departed operator's history to somebody else. Disable is the reversible operation; delete is not.",
    features: [
      "Create a user and receive a one-time password shown once.",
      "Reset a password, which seizes the account and ends its sessions immediately.",
      "Disable or re-enable an account; disabling revokes sessions rather than waiting for expiry.",
      "Delete a user, retiring the login permanently.",
      "See each user's group memberships and whether they still owe a password change.",
    ],
    howTo: [
      {
        task: "Add an operator",
        steps: [
          "Click 'Add user'.",
          "Enter a login — permanent, and used in every audit record.",
          "Click 'Create and show password', then copy the one-time password before dismissing the dialog. It cannot be shown again.",
          "Give it to them over a channel you trust; they will be required to change it at first sign-in.",
        ],
      },
      {
        task: "Offboard someone",
        steps: [
          "Prefer Disable: it ends their sessions immediately and keeps their group memberships, and it is reversible.",
          "Use Delete only when the login should never exist again — it is retired permanently.",
        ],
      },
      {
        task: "Help someone who forgot their password",
        steps: [
          "Open the row menu and choose 'Reset password'.",
          "Copy the new one-time password and pass it on.",
          "They are signed out everywhere and must choose a new password at next sign-in.",
        ],
      },
    ],
    related: ["identityGroups", "settings"],
    keywords: [
      "user", "users", "account", "login", "password", "reset", "disable", "delete", "offboard",
      "identity", "srp", "credential", "admin", "operator",
    ],
    aria: {
      label: "Users page",
      description: "Manage who can sign in, reset passwords, and disable or delete accounts.",
    },
    aiContext:
      "Users manages the local SRP identity store. Passwords are never stored — only verifiers — so created and reset accounts receive a one-time password shown exactly once and are flagged to change it at first sign-in. Logins are immutable and never reused after deletion, because audit records identify people by login. Disabling revokes sessions immediately and is reversible; deleting retires the login permanently. An administrator cannot disable or delete their own account, nor the last account able to sign in.",
  },
  {
    id: "identityGroups",
    kind: "view",
    title: "Groups",
    domain: "Identity",
    routePath: "/identity/groups",
    summary: "Organise users into flat groups; membership is how grants will be conferred.",
    description:
      "Groups collect users for authorization. Groups are flat — a group holds users and never another group — so there is no nesting to reason about and no inherited membership. Membership recorded now takes effect when the policy engine lands: today authorization is permissive for every signed-in user, so a group grants nothing yet and this page is not restricting access. A group name is its identifier and cannot be changed; deleting a group removes its memberships but leaves the users untouched.",
    features: [
      "Create and delete groups with an optional description.",
      "Add and remove members, choosing from existing users.",
      "See every group's membership at a glance.",
    ],
    howTo: [
      {
        task: "Create a group and add members",
        steps: [
          "Click 'Add group' and give it a name — permanent, since the name identifies it.",
          "Click 'Add member' on the group card.",
          "Pick a user. Only users can be members; a group never contains another group.",
        ],
      },
    ],
    related: ["identityUsers"],
    keywords: [
      "group", "groups", "membership", "member", "role", "team", "authorization", "policy",
      "permission", "access",
    ],
    aria: {
      label: "Groups page",
      description: "Organise users into groups and manage membership.",
    },
    aiContext:
      "Groups are flat collections of users for authorization: a group contains users and never another group, so membership is not transitive. Group names are identifiers and cannot be renamed. Membership confers no access yet — the authorization engine is permissive for any signed-in user until the policy engine is wired — so groups recorded now take effect then. Editing membership is the highest-privilege operation on this surface, because it is how grants will be conferred.",
  },
];
