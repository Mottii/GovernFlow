# Monthly reports

`GET /admin/reports/{client_id}/{YYYY-MM}` returns JSON. Add `?format=csv` for a download or `?format=html` for a printable page. Dashboard Reports provides both exports for an accessible client and the selected UTC month.

For calls created after migration 0019, totals use the current canonical charge in the original UTC budget month. A late measurement or administrator correction restates that month and counts the call once. Estimated charges remain included; estimated cost, estimated-call count, corrected-call count, and recovery backlog are visible. Ledger entries and original audit events remain unchanged. Monthly budget spend and reservations are listed separately.

Older calls retain their original audit-event settlement-month attribution. If a legacy settled reservation is adopted for queued recovery, reports add only the adjustment above its historical baseline and preserve the original call count. Workflow totals add to the client total, including an unassigned group for historical events whose run cannot be resolved. See [accounting recovery](phase2-accounting.md) for correction authorization and migration behavior.

Hours saved are supplied explicitly by an administrator or operator through `POST /admin/reports/{client_id}/{YYYY-MM}/hours`, with integer `minutes_saved`, `basis` (`measured` or `estimate`), and an evidence `note`. Migration 0014 stores these inputs and the editor identity. Updates enter the audit chain. Call counts do not imply completed business tasks, so they are not used to invent savings. Missing hours differ from an explicitly supplied zero.

Exports escape tenant text for HTML and spreadsheet formulas for CSV. All formats enforce the same tenant scope. Failed or missing provider reporting is identified as unknown; Gemini remains gateway-observed in v1. The HTML page supports the browser's Print command with A4 print styling.
