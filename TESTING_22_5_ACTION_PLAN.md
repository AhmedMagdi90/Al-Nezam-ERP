# Testing 22-5-26 Action Plan

Source file: `C:\Users\AFRO\Downloads\testing 22-5.docx`

## Recommended Solve Order

### 1. Company setup basics

- Company logo in settings is not working.
- Starting team on company creation screen is not added after creating the company.

Reason: These are foundational setup flows. If company bootstrap data is wrong, later imports and planning setup can be misleading.

### 2. Team import and team assignment imports

- Bulk upload team has the wrong app scope label/value: Planner should change to Manufacturing.
- Bulk upload team into products accepts the file without errors when it should validate and report problems.

Reason: Team data is used by products, planning, and assignment workflows. Validation must be fixed before relying on uploaded data.

### 3. Factory setup integrity

- Setting different working hours on one machine in a department does not update the rest of the machines in the same department.
- Edit Machine in factory setup changes some data and deletes other data.

Reason: Machine records and calendars are the resource base for scheduling. These must be stable before timeline issues can be trusted.

### 4. BOM and stage setup

- Bulk upload BOM needs a full Excel file rework.
- Stage in factory setup: adding a new stage after creating a BOM for existing stages needs verification/fix.

Reason: BOM/stage definitions drive work-order routing and planning calculations.

### 5. Work order and material planning

- Created work order and confirmed material in planning needs verification/fix.

Reason: This connects BOM/stage data to executable planning. It should be checked after setup data is stable.

### 6. Planner action center

- All buttons in the Planner action center are not working.

Reason: Action buttons likely depend on valid work-order state. Fix after work-order planning flow is reproducible.

### 7. Planner timeline rendering and scheduling bugs

- After adding 2 machines, the machines appeared on the timeline. After adding the rest through bulk upload and add machine, old and new machines were removed from the timeline and this error appeared:
  - `Timeline Init Error: Cannot read properties of null (reading 'start')`
- Timeline works when changing to month view.
- Splitting work order from timeline works, but timeline placement is incorrect: same order, same time, different timeline view.
- Screenshot evidence shows the same work order with inconsistent stage time/end date between views:
  - one view: stage time `6 days 22 hours 55 minutes`, end `6/11/2026, 10:47:30 AM`
  - another view: stage time `6 days 22 hours 55 minutes`, end `6/7/2026, 11:24:00 PM`

Reason: Timeline issues depend on valid machines, calendars, stages, and work orders. Fixing them last avoids chasing symptoms caused by corrupt setup data.

## Execution Rule

Solve and verify one numbered section at a time. Do not move to the next section until the current section has a reproducible before/after check.
