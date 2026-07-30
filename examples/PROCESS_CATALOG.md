# Process Catalog — Canonical Salesforce Capture Candidates

This catalog lists ten Salesforce processes that are well-suited to DOM capture.
Use it to decide what to record next and to understand what the pipeline can
derive from each recording.

For each process the table shows:
- **Objects touched** — the Salesforce objects whose records you interact with
- **Expected event count** — approximate number of raw DOM events a clean
  single-step recording produces (clicks + inputs + navigation combined)
- **What the pipeline can derive** — what `build_agent_spec` produces when the
  recording is complete; entries marked "only with live-org telemetry" require
  `--mode live` to earn them

---

## Catalog

### 1. Case Creation *(has AFT3 capture)*

**Process:** Open the Cases list view → New → fill Subject, Description, Status,
Priority, Case Origin → Save.

| Attribute | Value |
|-----------|-------|
| Objects touched | Case |
| Expected event count | 140 – 200 |
| Evidence available | Real capture in `examples/case_creation_aft3.dom_capture.jsonl` |

**What the pipeline can derive:**
- Intent: `Create Case` with entity list `{Subject, Description, Status, Priority, Case Origin}`
- One orchestration step per filled field plus a commit step
- Guardrails: required-field check (Subject) and modal-dismissal guard
- `selector_confidence` on the Subject field is 0.85 (tier 3 — role+name via
  `role_name.name="Subject"`) because the recorder emits no `data-testid`
- With live-org telemetry: backend record-creation confirmation (`INSERT` event
  on Case object) to elevate `evidence_grounding` from inference to data-delta

> **Note:** This capture currently does not parse cleanly. 171 of 175 events are
> rejected because `RawRoleName.role` was non-nullable and the recorder emits
> `role: null` for LWC custom elements. Defect L4-1 was fixed in lane 04
> (2026-07-26). The file is kept as a regression fixture. Run the pipeline on it
> with the current parser to measure the improvement.

---

### 2. Case Triage (Status Update) *(has synthetic capture)*

**Process:** Open an existing Case record → change Status → Save.

| Attribute | Value |
|-----------|-------|
| Objects touched | Case |
| Expected event count | 6 – 12 |
| Evidence available | Synthetic capture in `examples/case_triage.dom_capture.jsonl` |

**What the pipeline can derive:**
- Intent: `Update Case (Status)` at confidence 0.70
- Entity: `Status` (picklist change)
- Spec scores 79/100 with mock telemetry (blocked; see `examples/README.md` for
  the full score breakdown)
- With live-org telemetry: field-change `before`/`after` snapshot raises
  `evidence_grounding` to the data-delta tier

---

### 3. Lead Conversion

**Process:** Open a Lead record → Convert → map to Account/Contact/Opportunity →
Convert button.

| Attribute | Value |
|-----------|-------|
| Objects touched | Lead, Account, Contact, Opportunity |
| Expected event count | 20 – 40 |
| Evidence available | None — good next capture target |

**What the pipeline can derive:**
- Intent: `Convert Lead` with cross-object entity map
- Multi-object orchestration: one step per created record type
- Guardrails: duplicate-check guard (Account/Contact merge dialog), required
  Opportunity Name warning
- With live-org telemetry: confirmation that all three child records were
  inserted (Lead conversion is a single atomic DML — three `INSERT` events in
  the telemetry proves success; zero `INSERT` events after Save reveals a
  validation failure the recording alone cannot show)

---

### 4. Opportunity Stage Advancement

**Process:** Open an Opportunity → change Stage → update Close Date and
Probability → Save.

| Attribute | Value |
|-----------|-------|
| Objects touched | Opportunity |
| Expected event count | 15 – 30 |
| Evidence available | None |

**What the pipeline can derive:**
- Intent: `Advance Opportunity Stage` with entities `{Stage, CloseDate, Probability}`
- Field-dependency guardrail: probability auto-populates when stage changes
  (pipeline detects the input event was dropped after the stage `change`)
- With live-org telemetry: `UPDATE` on Opportunity with field delta confirms
  the stage change persisted, and `stage_change_history` can surface the
  previous stage name for the entity evidence

---

### 5. Contact Creation

**Process:** Navigate to Contacts → New → fill First Name, Last Name, Account
Name, Phone, Email → Save.

| Attribute | Value |
|-----------|-------|
| Objects touched | Contact, Account |
| Expected event count | 20 – 40 |
| Evidence available | None |

**What the pipeline can derive:**
- Intent: `Create Contact` with entity list `{FirstName, LastName, Account, Phone, Email}`
- Email is a sensitive field — the recorder marks `value_redacted: true` and
  `validate_trace` checks for leaks via the L4-6 hardened pattern list
- Guardrail: required Last Name alert
- With live-org telemetry: `INSERT` on Contact confirms the record was saved;
  the returned `Id` binds the entity `recordId` to observed evidence

---

### 6. Account Merge

**Process:** Open an Account → Find Duplicates → select merge candidate →
choose master record → Merge.

| Attribute | Value |
|-----------|-------|
| Objects touched | Account |
| Expected event count | 25 – 50 |
| Evidence available | None |

**What the pipeline can derive:**
- Intent: `Merge Duplicate Accounts`
- Two-entity pattern: `masterRecordId` and `duplicateRecordId`
- Pipeline derives a two-step orchestration: identify duplicate, confirm merge
- Guardrail: irreversibility warning (merge cannot be undone)
- With live-org telemetry: `DELETE` on the non-master Account record and
  `UPDATE` on the master (to absorb child records) together prove the merge
  completed rather than just opened the dialog

---

### 7. Service Appointment Scheduling (Field Service)

**Process:** Open a Work Order → New Service Appointment → set Scheduled Start /
End → Assign to Service Resource → Dispatch.

| Attribute | Value |
|-----------|-------|
| Objects touched | WorkOrder, ServiceAppointment, ServiceResource |
| Expected event count | 30 – 60 |
| Evidence available | None |

**What the pipeline can derive:**
- Intent: `Schedule Service Appointment` with date-range and assignee entities
- Multi-record orchestration (parent Work Order → child Appointment)
- `selector_confidence` is typically low (0.35 – 0.60) on Field Service
  Lightning pages because they render inside an iframe with deep shadow DOM and
  few stable ARIA landmarks — the capture will produce tier-7/8 CSS selectors
- With live-org telemetry: `INSERT` on ServiceAppointment plus an
  `AssignedResource` junction record confirms the dispatch, not just the save

---

### 8. Approval Process Submission

**Process:** Open an Opportunity or custom record → Submit for Approval →
enter comments → Submit.

| Attribute | Value |
|-----------|-------|
| Objects touched | Opportunity (or custom object), ProcessInstance, ProcessInstanceStep |
| Expected event count | 10 – 20 |
| Evidence available | None |

**What the pipeline can derive:**
- Intent: `Submit Record for Approval`
- Entity: `comments` (text area — typically short, not redacted)
- Guardrail: one-active-approval-at-a-time constraint
- With live-org telemetry: `INSERT` on `ProcessInstance` and `ProcessInstanceStep`
  proves submission reached the server; the approval route (approver names) is
  in the `ProcessInstanceStep` records and rounds out entity evidence

---

### 9. Mass Email Send (List Email)

**Process:** Navigate to a Campaign Members list → Send List Email → fill
Subject, Body → choose Email Template → Send Now.

| Attribute | Value |
|-----------|-------|
| Objects touched | Campaign, Contact/Lead, ListEmail |
| Expected event count | 25 – 50 |
| Evidence available | None |

**What the pipeline can derive:**
- Intent: `Send Mass Email to Campaign Members`
- Entities: `Subject`, `EmailTemplate`, recipient `CampaignId`
- Email body is a sensitive field — the pipeline will flag value_redacted
  warnings if the recorder marks it; if it does not, `validate_trace` catches
  the leak via the L4-6 `email` pattern
- Guardrail: send-to-unsubscribed-contacts check
- With live-org telemetry: `INSERT` on `ListEmail` and related
  `CampaignMemberStatus` updates confirm delivery was scheduled

---

### 10. Custom Lightning Component Interaction

**Process:** Navigate to a page with a custom LWC component → interact with
its internal inputs, buttons, and picklists → submit.

| Attribute | Value |
|-----------|-------|
| Objects touched | Custom object (varies) |
| Expected event count | 10 – 40 |
| Evidence available | None |

**What the pipeline can derive:**
- This is the hardest capture scenario. Custom LWCs typically lack
  `data-testid` attributes and sometimes have no ARIA labels on internal
  elements. The recorder emits `role: null` / `name: null` for such elements;
  after the L4-1 fix the parser accepts these events, but selector confidence
  falls to 0.35 (tier-8 XPath or tier-7 CSS-only)
- Intent is derivable when the component wraps a standard record form; it is
  `UNRESOLVED:` when the only signals are raw DOM class names
- With live-org telemetry: observing the DML event (object + operation) via
  Event Monitoring or REST polling gives the pipeline a signal independent of
  the DOM, letting it ground the intent even when selectors are weak

---

## How to use this catalog

1. **Pick a process** from the catalog whose objects you have access to in a
   scratch or sandbox org.
2. **Estimate event count** before recording. If the real count is far outside
   the range, the recording likely captured a second process or missed a step.
3. **Check `selector_confidence`** on the result (see `examples/README.md` for
   the definition). Tier 1-2 selectors (test_id, role+name) are stable and
   replay-safe. Tier 7-8 selectors (CSS path, XPath) break on org customisation.
4. **Add live-org telemetry** if you need the spec to pass the quality gate.
   Mock-telemetry runs score around 79/100 and are blocked regardless of how
   good the DOM capture is.
5. **Add the resulting file to `examples/`** under the rules in
   `examples/README.md`: synthetic or real-and-redacted, never raw.
