"""Architecture-neutral workflow graph definitions."""

from labbioagentos.contracts import (
    WorkflowDefinition,
    WorkflowStage,
    WorkflowTransition,
)


def default_workflow_definition() -> WorkflowDefinition:
    """Return the default workflow as data, not engine control logic."""

    nodes = frozenset(
        {
            WorkflowStage.INTAKE,
            WorkflowStage.UNDERSTAND,
            WorkflowStage.PLAN,
            WorkflowStage.PREFLIGHT,
            WorkflowStage.EXECUTE,
            WorkflowStage.VALIDATE,
            WorkflowStage.INTERPRET,
            WorkflowStage.REPORT,
            WorkflowStage.LEARN,
        }
    )
    transitions = frozenset(
        {
            WorkflowTransition(source=WorkflowStage.INTAKE, target=WorkflowStage.UNDERSTAND),
            WorkflowTransition(source=WorkflowStage.UNDERSTAND, target=WorkflowStage.PLAN),
            WorkflowTransition(source=WorkflowStage.PLAN, target=WorkflowStage.PREFLIGHT),
            WorkflowTransition(source=WorkflowStage.PREFLIGHT, target=WorkflowStage.EXECUTE),
            WorkflowTransition(source=WorkflowStage.EXECUTE, target=WorkflowStage.VALIDATE),
            WorkflowTransition(source=WorkflowStage.VALIDATE, target=WorkflowStage.INTERPRET),
            WorkflowTransition(source=WorkflowStage.INTERPRET, target=WorkflowStage.REPORT),
            WorkflowTransition(source=WorkflowStage.REPORT, target=WorkflowStage.LEARN),
        }
    )
    return WorkflowDefinition(
        workflow_id="labbio-default-v1",
        nodes=nodes,
        allowed_transitions=transitions,
        initial_stage=WorkflowStage.INTAKE,
        terminal_stages=frozenset({WorkflowStage.LEARN}),
    )

