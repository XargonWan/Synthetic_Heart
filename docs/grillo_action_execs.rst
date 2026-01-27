Grillo action executions
=======================

This document describes the new `grillo_action_execs` table and the WebUI
exposure added to track individual action proposals and execution status
created by the Grillo action checker.

Table: grillo_action_execs
-------------------------

- id: Primary key
- activity_log_id: FK to `grillo_activity_log.id`
- action_index: Index (order) inside the suggested actions array
- action_type: The action type string (e.g., `schedule_message`)
- payload: JSON payload of the proposed action
- status: Enum(`pending`, `processed`, `failed`)
- error_text: human-readable error text when status=`failed`
- result: optional JSON with execution metadata
- created_at / updated_at timestamps

WebUI
-----

The `History -> Grillo` endpoint now includes an `actions` array inside each
entry returned by `/api/history/grillo`. Each `actions` item contains:

- id
- activity_log_id
- action_index
- action_type
- payload
- status
- error_text
- result
- created_at

The History → Grillo UI renders these action entries under each Grillo card,
including status chips (pending/processed/failed) and payload/result details
to help monitor post-chain action proposals.

Usage
-----

- When Grillo proposes actions (GRILLO_AUTO_GENERATE_ACTIONS=False), each
  proposed action is persisted with `status='pending'`.
- When Grillo auto-executes actions (GRILLO_AUTO_GENERATE_ACTIONS=True), the
  action rows are created with `status='processed'` or `status='failed'`
  depending on the result returned by `core.action_parser.run_actions`.
