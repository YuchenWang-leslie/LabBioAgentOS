"""Explicit local composition of the existing governed application runtime."""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import tomllib
from typing import Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, model_validator

from labbioagentos.literature import LiteratureSearchService
from labbioagentos import (
    ApplicationExecutionProfile, ApplicationRuntimeConfiguration, ApprovedImage,
    CapabilityProfile, ExecutionPolicy, ExecutionRuntime, JsonlTraceSink,
    LabBioApplication, ModelProfile, Principal, Project, PromptProfile,
    ProviderConfigRef, ProviderThinkingWireFormat, ProviderTransport,
    RequestedResources, ResponseSchemaRef, RuntimeProfileCatalog,
    RuntimeStageAssemblySpec, SQLiteRunStateStore, StructuredOutputContract,
    WorkflowStage, WorkspaceContext, default_agent_profiles,
)


class _SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LocalProviderSettings(_SettingsModel):
    env_file: Path
    api_key_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    base_url_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    model_identifier: str = Field(min_length=1, max_length=256)
    thinking_enabled: bool = False
    interpretation_thinking_enabled: bool = True
    provider_tool_schema_strict: bool = False
    max_output_tokens: int = Field(default=16_384, ge=256, le=32_768)


class LocalExecutionSettings(_SettingsModel):
    image_key: str
    image_reference: str
    available_python_modules: tuple[str, ...] = ()
    installed_packages: dict[str, str] = Field(default_factory=dict)
    resources: RequestedResources
    max_output_file_bytes: int = Field(default=16_777_216, ge=1, strict=True)
    max_collected_output_bytes: int = Field(default=67_108_864, ge=1, strict=True)
    tmpfs_size_mb: int = Field(default=64, ge=1, strict=True)

    def approved_image(self) -> ApprovedImage:
        return ApprovedImage(
            key=self.image_key, reference=self.image_reference,
            runtime=ExecutionRuntime.PYTHON,
            available_python_modules=self.available_python_modules,
            installed_packages=self.installed_packages,
        )

    @model_validator(mode="after")
    def validate_image(self) -> "LocalExecutionSettings":
        self.approved_image()
        if self.max_output_file_bytes > self.max_collected_output_bytes:
            raise ValueError("Total collection limit must be at least the per-file limit")
        return self


class LocalEnvironmentSettings(_SettingsModel):
    root: Path
    build_proxy: str | None = None
    build_timeout_seconds: float = Field(default=600.0, gt=0, le=3600)

    @model_validator(mode="after")
    def validate_proxy(self) -> "LocalEnvironmentSettings":
        if self.build_proxy is not None:
            parsed = urlsplit(self.build_proxy)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or any(char.isspace() for char in self.build_proxy)):
                raise ValueError("Build proxy must be an HTTP(S) endpoint without credentials")
        return self


class LocalSettings(_SettingsModel):
    result_root: Path
    input_roots: tuple[Path, ...] = Field(min_length=1, max_length=64)
    identity: WorkspaceContext
    provider: LocalProviderSettings
    execution: LocalExecutionSettings
    default_format: Literal["raw", "h5ad"] = "raw"
    profile: Path | None = None
    managed_root: Path | None = None
    gold_root: Path | None = None
    environment: LocalEnvironmentSettings | None = None

    @property
    def principal(self) -> Principal:
        return Principal(user_id=self.identity.user_id, lab_id=self.identity.lab_id)

    @property
    def workspace(self) -> WorkspaceContext:
        return self.identity


class _StageProfile(_SettingsModel):
    stage: WorkflowStage
    root: str
    capabilities: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    capability_protocol: str = ""
    user_input_enabled: bool = False
    clarification_enabled: bool = True


class _LocalProfile(_SettingsModel):
    version: str
    deployment_context: str
    capability_common: str
    finalization_protocol: str
    stages: tuple[_StageProfile, ...]
    output_contract: StructuredOutputContract


def load_settings(path: Path) -> LocalSettings:
    """Read non-secret TOML once; resolve relative paths against its directory."""
    path = path.expanduser().resolve()
    settings = LocalSettings.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))

    def absolute(value: Path) -> Path:
        value = value.expanduser()
        return (value if value.is_absolute() else path.parent / value).resolve()

    managed_root = settings.managed_root
    if managed_root is not None:
        managed_root = managed_root.expanduser()
        managed_root = (managed_root if managed_root.is_absolute() else path.parent / managed_root).absolute()
    return settings.model_copy(update={
        "result_root": absolute(settings.result_root),
        "input_roots": tuple(absolute(value) for value in settings.input_roots),
        "profile": absolute(settings.profile) if settings.profile else None,
        "managed_root": managed_root,
        "gold_root": absolute(settings.gold_root) if settings.gold_root else None,
        "environment": settings.environment.model_copy(update={
            "root": absolute(settings.environment.root),
        }) if settings.environment else None,
        "provider": settings.provider.model_copy(update={"env_file": absolute(settings.provider.env_file)}),
    })


def _profile_bytes(settings: LocalSettings) -> bytes:
    path = settings.profile or Path(__file__).with_name("resources") / "local-default.json"
    return path.read_bytes()


def _source_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def runtime_manifest(settings: LocalSettings) -> dict:
    """Content-derived identity; provider env values are neither read nor retained."""
    import pantheon

    profile_bytes = _profile_bytes(settings)
    profile = _LocalProfile.model_validate_json(profile_bytes)
    if settings.gold_root is not None:
        # Local composition only: expose existing governed Gold tools, never
        # select a Skill or authorize its use on the Agent's behalf.
        profile = profile.model_copy(update={"stages": tuple(
            stage.model_copy(update={
                "capabilities": tuple(dict.fromkeys((*stage.capabilities,
                    "skill_search", "skill_propose_use", "skill_view"))),
                "user_input_enabled": True,
            }) if stage.stage is WorkflowStage.PLAN else stage
            for stage in profile.stages
        )})
    if settings.environment is not None:
        environment_tools = {
            WorkflowStage.PLAN: ("environment_list",),
            WorkflowStage.EXECUTE: ("environment_list", "environment_build"),
        }
        profile = profile.model_copy(update={"stages": tuple(
            stage.model_copy(update={"capabilities": tuple(dict.fromkeys((
                *stage.capabilities, *environment_tools.get(stage.stage, ()),
            )))}) for stage in profile.stages
        )})
    effective_profile = profile.model_dump(mode="json")
    # Set iteration order must not change the revision across process restarts.
    for field in ("allowed_fields", "required_fields"):
        effective_profile["output_contract"][field] = sorted(effective_profile["output_contract"][field])
    manifest = {
        "format_version": 1,
        "configuration": settings.model_dump(mode="json"),
        "profile": effective_profile,
        "profile_sha256": hashlib.sha256(profile_bytes).hexdigest(),
        "source": {
            "labbio_sha256": _source_digest(Path(__file__).parent),
            "pantheon_sha256": _source_digest(Path(pantheon.__file__).parent),
        },
    }
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    manifest["runtime_revision"] = "local-" + hashlib.sha256(encoded).hexdigest()
    return manifest


def _load_provider(settings: LocalProviderSettings) -> None:
    if not settings.env_file.is_file():
        raise ValueError("Configured provider environment file is unavailable")
    values = dotenv_values(settings.env_file, interpolate=False)
    key, endpoint = values.get(settings.api_key_env), values.get(settings.base_url_env)
    if not key or not endpoint or any(character in key + endpoint for character in "\r\n"):
        raise ValueError("Configured provider key or endpoint is missing or invalid")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("Configured provider endpoint is invalid")
    os.environ["OPENAI_API_KEY"] = key
    os.environ["OPENAI_API_BASE"] = endpoint


def build_application(
    settings: LocalSettings, run_root: Path, *, load_provider: bool = True,
) -> LabBioApplication:
    """Assemble known owners without selecting methods or installing dependencies."""
    manifest = runtime_manifest(settings)
    profile = _LocalProfile.model_validate_json(json.dumps(manifest["profile"]))
    if load_provider:
        _load_provider(settings.provider)
    run_root = run_root.expanduser().resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    defaults = default_agent_profiles()
    # A separate catalog binding keeps the shared Coordinator's other stages
    # unchanged, including both capability and finalization model parameters.
    interpretation = defaults[0].model_copy(update={
        "profile_key": "interpretation", "agent_name": "InterpretationAgent",
        "role_description": "Interpret accessible results and external literature within the current stage contract.",
        "model_profile_key": "runtime-interpretation",
        "capability_profile_key": "interpretation-capabilities",
    })
    profiles = (*defaults, interpretation)
    capabilities = {
        agent.profile_key: tuple(sorted({
            capability for stage in profile.stages if stage.root == agent.profile_key
            for capability in stage.capabilities
        }))
        for agent in profiles
    }
    catalog = RuntimeProfileCatalog(
        agents=profiles,
        prompts=(PromptProfile(template_id="runtime-generic", version=profile.version,
                               template_text="{protocol}", max_value_length=8_000),),
        models=tuple(ModelProfile(
            profile_key=key, version=profile.version,
            model_identifier=settings.provider.model_identifier,
            provider_config=ProviderConfigRef(config_id="local-provider", provider="openai-compatible"),
            transport=ProviderTransport.OPENAI_CHAT_COMPLETIONS,
            thinking_enabled=thinking,
            private_tool_reasoning_continuity=thinking,
            provider_tool_schema_strict=settings.provider.provider_tool_schema_strict,
            thinking_wire_format=ProviderThinkingWireFormat.TYPE_OBJECT,
            max_output_tokens=settings.provider.max_output_tokens,
        ) for key, thinking in (
            ("runtime-default", settings.provider.thinking_enabled),
            ("runtime-interpretation", settings.provider.interpretation_thinking_enabled),
        )),
        schemas=(ResponseSchemaRef(),),
        capabilities=tuple(CapabilityProfile(
            profile_key=agent.capability_profile_key, version=profile.version,
            capability_allowlist=capabilities[agent.profile_key],
        ) for agent in profiles),
    )
    owners = {}
    for stage in profile.stages:
        for capability in stage.capabilities:
            owners.setdefault(capability, []).append(stage.stage.value)
    shared_context = (
        profile.deployment_context
        + "\nThis catalog describes configured ownership, does not grant tools in the current phase, "
        "and is not a required action sequence. Current typed control remains authoritative.\n"
        + "CONFIGURED_CAPABILITY_OWNERS=" + json.dumps(owners, sort_keys=True)
        + "\n"
    )
    assemblies = tuple(RuntimeStageAssemblySpec(
        stage_id=stage.stage, root_profile_key=stage.root,
        prompt_template_key="runtime-generic", capability_allowlist=stage.capabilities,
        capability_prompt_values={"protocol": (
            shared_context + profile.capability_common.format(stage=stage.stage.value)
            + " " + stage.capability_protocol
        )} if stage.capabilities else {},
        finalization_prompt_values={"protocol": (
            shared_context + profile.finalization_protocol.format(stage=stage.stage.value)
        )},
        capability_phase_enabled=bool(stage.capabilities),
        required_capabilities=stage.required_capabilities,
        max_capability_turns=16, retry_enabled=stage.stage is not WorkflowStage.VALIDATE,
        user_input_enabled=stage.user_input_enabled,
        clarification_enabled=stage.clarification_enabled,
    ) for stage in profile.stages)

    def observe(kind: str, value: object) -> None:
        payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        with (run_root / "model-boundaries.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": kind, "payload": payload}, sort_keys=True) + "\n")

    resources = settings.execution.resources
    environment_service_factory = None
    if settings.environment is not None:
        from .execution.environment_builder import DockerEnvironmentBuilder
        from .execution.environments import EnvironmentService

        def environment_service_factory(registry):
            return EnvironmentService(
                root=settings.environment.root, image_registry=registry,
                builder=DockerEnvironmentBuilder(
                    proxy_url=settings.environment.build_proxy,
                    timeout_seconds=settings.environment.build_timeout_seconds,
                ),
                owner_user_id=settings.principal.user_id,
            )

    with ExitStack() as cleanup:
        skill_service = None
        handlers = ()
        if settings.gold_root is not None:
            from .application import SkillDomainDecisionHandler
            from .local_gold import build_personal_gold_service, close_personal_gold

            skill_service = build_personal_gold_service(settings.gold_root, settings.principal.user_id)
            cleanup.callback(close_personal_gold, skill_service)
            handlers = (SkillDomainDecisionHandler(skill_service),)
        run_store = SQLiteRunStateStore(run_root / "state.sqlite")
        cleanup.callback(run_store.close)
        application = LabBioApplication(ApplicationRuntimeConfiguration(
            artifact_root=run_root / "artifacts", execution_workspace_root=run_root / "executions",
            runtime_revision=manifest["runtime_revision"], allowed_input_roots=settings.input_roots,
            projects=(Project(project_id=settings.workspace.project_id, lab_id=settings.workspace.lab_id,
                              owner_user_id=settings.principal.user_id),),
            profile_catalog=catalog, stage_assemblies=assemblies,
            approved_images=(settings.execution.approved_image(),), output_contracts=(profile.output_contract,),
            execution_policy=ExecutionPolicy(
                allow_network=False, max_cpus=resources.cpus, max_memory_mb=resources.memory_mb,
                max_pids=resources.pids_limit, max_timeout_seconds=resources.timeout_seconds,
                max_output_file_bytes=settings.execution.max_output_file_bytes,
                max_collected_output_bytes=settings.execution.max_collected_output_bytes,
                tmpfs_size_mb=settings.execution.tmpfs_size_mb,
            ),
            execution_profile=ApplicationExecutionProfile(
                runtime=ExecutionRuntime.PYTHON, image_key=settings.execution.image_key, resources=resources,
                network_required=False, output_contract_ids=(profile.output_contract.contract_id,),
                minimum_queryable_output_count=1,
            ),
            trace_sink=JsonlTraceSink(run_root / "run-trace.jsonl"),
            run_state_store=run_store,
            boundary_observer=observe, retry_limit=1,
            skill_service=skill_service, domain_decision_handlers=handlers,
            environment_service_factory=environment_service_factory,
            literature_search=LiteratureSearchService(),
        ))
        cleanup.pop_all()  # The caller now owns both stores, including failure cleanup.
        return application
