"""The workflow registry. Adding a workflow adds its package and one entry here."""
from .implement import WORKFLOW as IMPLEMENT
from .plan import WORKFLOW as PLAN

WORKFLOWS = {workflow.name: workflow for workflow in (PLAN, IMPLEMENT)}
