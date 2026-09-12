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
  {
    id: "identityPolicies",
    kind: "view",
    title: "Policies",
    domain: "Identity",
    routePath: "/identity/policies",
    summary: "Write authorization policies and attach them to users and groups.",
    description:
      "Policies decide what a signed-in operator may do. Evaluation starts at an implicit deny: a request is refused unless some attached policy allows it, and any matching deny wins outright — a deny cannot be outvoted by any number of allows, at any specificity, in any file order. Policies never name principals; they are attached to users and groups as a separate relation, so granting someone access never means editing a document other people depend on. A policy that is not attached to anyone grants nothing. With no policies at all, every authenticated request is permitted and the audit trail records the engine as permissive, so writing the first policy is what starts enforcement.",
    features: [
      "Author policies in a JSON editor that validates against the server's own schema as you type.",
      "Completion over the real action vocabulary, so an action name cannot be mistyped silently.",
      "Attach a policy to a user or a group; attaching to a group reaches every member.",
      "See which subjects hold each policy before changing or deleting it.",
    ],
    howTo: [
      {
        task: "Grant a team the ability to power-cycle racks",
        steps: [
          "Click 'New policy'. The template shows an allow plus a deny, which is the usual shape.",
          "Name the actions (for example rack.power:*) and the resources (rack.power:a01, or rack.power:* for all).",
          "Save. The server validates again and refuses a bare '*' in an allow statement.",
          "Click 'Attach' and pick a group. Until then the policy grants nothing.",
        ],
      },
      {
        task: "Protect a rack from an existing grant",
        steps: [
          "Add a deny statement naming that rack, in the same policy or a separate one.",
          "Attach it to the same subjects. A deny beats every allow, so ordering does not matter.",
        ],
      },
      {
        task: "Check what a policy actually grants, before trusting it",
        steps: [
          "Open GET /api/policies/preview (add ?format=table for a readable grid).",
          "It walks the whole action vocabulary, marks everything denied, then applies the policies attached to that principal exactly as the engine does — denies last and not flippable.",
          "Each allowed entry names the resource patterns that qualify it and the policy and statement that decided, so the answer is 'yes, for these racks' rather than a bare true.",
        ],
      },
      {
        task: "Let a team adopt shared boards without overwriting your tools",
        steps: [
          "Allow catalog.board:import together with catalog.tool:create and workflow:save.",
          "Deny catalog.tool:update in the same policy.",
          "Importing a shared board then creates actions the instance lacks and refuses any item that would replace one of yours; the refused item is reported as skipped and the rest still apply.",
        ],
      },
    ],
    faqs: [
      {
        q: "Why am I getting 403 when I am signed in?",
        a: "403 means authorization, not authentication: you are who you say you are and may not do this. Most often no attached policy grants the action. Check the audit event — it carries the deciding reason — or open GET /api/policies/preview for the whole picture for that principal. A 401 instead means the credential itself was refused, which is a different problem.",
      },
      {
        q: "A deny is refusing me and I cannot see which rule.",
        a: "Give every statement a 'sid'. The audit reason is then 'denied by <policy>/<sid>', which names the exact rule. Without a sid the verdict can only name the document.",
      },
      {
        q: "A brand-new feature returns 403 for an administrator.",
        a: "The administrators policy can be narrower than the product after an upgrade that adds actions. Run 'python backend/cli.py reconcile-admin-policy' to add the newer resource types to it.",
      },
      {
        q: "My grant on one rack does not let the table load.",
        a: "A list verb resolves to the collection sentinel '<type>:_all', and a grant on a single instance does not cover it — deliberately, because listing reveals what exists. Grant '<type>:*', which does match _all, or name _all explicitly.",
      },
    ],
    tips: [
      "Attach to a group rather than to each person: one write per role, and adding someone to the team grants it by membership.",
      "Write rack ids in lower case. Handlers that fold case have their resources folded to match, so a deny on 'rack.power:p17-r01' is what matches a request for 'P17-R01'.",
      "Unknown fields in a policy are rejected, not ignored — a misspelled key is a typo you want to hear about rather than a rule that silently does nothing.",
      "Full reference, with the schema field by field and worked examples: the Documentation site this deployment serves at /docs/ (Identity and authorization). The live schema and action list are also served at /api/policies/schema and /api/policies/vocabulary.",
    ],
    related: ["identityUsers", "identityGroups"],
    keywords: [
      "policy", "policies", "authorization", "authorisation", "rbac", "permission", "permissions",
      "grant", "deny", "allow", "attach", "attachment", "access control", "statement", "effect",
      "action", "resource", "role",
    ],
    aria: {
      label: "Policies page",
      description: "Write authorization policies and attach them to users and groups.",
    },
    aiContext:
      "Policies are the authorization layer. Evaluation is default-deny, deny-overrides-allow, and allow-only-on-explicit-match. Policies do not name principals: they are attached to users and groups, so cost grows with what is attached to the requesting principal rather than with the size of the whole policy set, and 'attach a policy to a user' never means editing a shared document. An unattached policy grants nothing, and with no policy file at all the engine is permissive and says so in the audit record. Actions are 'service[.subservice]:verb' (rack.power:on) and resources are '<type>:<id>' (rack.power:a01), with '<type>:_all' meaning the collection — so a grant of one rack does not confer the ability to list them. A bare '*' is refused in an allow statement because it would grant every action the product will ever have; it is permitted in a deny. Both the JSON schema and the action vocabulary are served by the API so the editor cannot accept a document the server would reject.",
    },
];
