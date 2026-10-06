# Monthly reports

`GET /admin/reports/{client_id}/{YYYY-MM}` returns JSON. Add `?format=csv` for a download or `?format=html` for a printable page. Dashboard Reports provides both exports for an accessible client and the selected UTC month.

Cost totals are the sum of `provider_call` audit-event costs settled in the UTC month. Estimated charges remain included and their count is displayed. Workflow totals add exactly to the client total, including an unassigned group for audit events whose run cannot be resolved to that client's workflows. Monthly budget spend and reservations are listed separately: a reservation made in one month can settle in the next.

Hours saved are supplied explicitly by an administrator or operator through `POST /admin/reports/{client_id}/{YYYY-MM}/hours`, with integer `minutes_saved`, `basis` (`measured` or `estimate`), and an evidence `note`. Migration 0014 stores these inputs and the editor identity. Updates enter the audit chain. Call counts do not imply completed business tasks, so they are not used to invent savings. Missing hours differ from an explicitly supplied zero.

Exports escape tenant text for HTML and spreadsheet formulas for CSV. All formats enforce the same tenant scope. Failed or missing provider reporting is identified as unknown; Gemini remains gateway-observed in v1. The HTML page supports the browser's Print command with A4 print styling.
