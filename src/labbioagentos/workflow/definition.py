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


def runtime_workflow_definition() -> WorkflowDefinition:
    """Return the reviewed first-runtime graph, including source-resuming gates."""

    main_path = (
        WorkflowStage.INTAKE,
        WorkflowStage.UNDERSTAND,
        WorkflowStage.PLAN,
        WorkflowStage.PREFLIGHT,
        WorkflowStage.EXECUTE,
        WorkflowStage.VALIDATE,
        WorkflowStage.INTERPRET,
        WorkflowStage.REPORT,
        WorkflowStage.LEARN,
    )
    gate_sources = frozenset(
        {
            WorkflowStage.INTAKE,
            WorkflowStage.UNDERSTAND,
            WorkflowStage.PLAN,
            WorkflowStage.PREFLIGHT,
            WorkflowStage.VALIDATE,
            WorkflowStage.LEARN,
        }
    )
    transitions = {
        WorkflowTransition(source=source, target=target)
        for source, target in zip(main_path, main_path[1:])
    }
    transitions.add(
        WorkflowTransition(
            source=WorkflowStage.VALIDATE,
            target=WorkflowStage.EXECUTE,
        )
    )
    for source in gate_sources:
        transitions.add(
            WorkflowTransition(source=source, target=WorkflowStage.USER_GATE)
        )
        transitions.add(
            WorkflowTransition(source=WorkflowStage.USER_GATE, target=source)
        )
    return WorkflowDefinition(
        workflow_id="labbio-runtime-v1",
        nodes=frozenset({*main_path, WorkflowStage.USER_GATE}),
        allowed_transitions=frozenset(transitions),
        initial_stage=WorkflowStage.INTAKE,
        terminal_stages=frozenset({WorkflowStage.LEARN}),
    )
