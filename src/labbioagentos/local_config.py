"""Explicit local composition of the existing governed application runtime."""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
import re
from pathlib import Path
import stat
import tomllib
from typing import Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import (
    BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator,
    model_serializer, model_validator,
)
from pydantic_core import PydanticCustomError

from labbioagentos.literature import LiteratureSearchService
from labbioagentos.artifacts import ExposurePolicy
from labbioagentos import (
    ApplicationExecutionProfile, ApplicationRuntimeConfiguration, ApprovedImage,
    CapabilityProfile, ExecutionPolicy, ExecutionRuntime, JsonlTraceSink,
    LabBioApplication, ModelProfile, Principal, Project, PromptProfile,
    ProviderConfigRef, ProviderThinkingWireFormat, ProviderTransport,
    RequestedResources, ResponseSchemaRef, RuntimeProfileCatalog,
    RuntimeBackend, RuntimeStageAssemblySpec, SQLiteRunStateStore, StructuredOutputContract,
    WorkflowStage, WorkspaceContext, default_agent_profiles,
)
from labbioagentos.kernel import (
    AnthropicMessagesDialect, GeminiGenerateContentDialect, GeminiInteractionsDialect,
    OpenAIChatDialect, OpenAIResponsesDialect, ProviderBinding, create_provider,
)
from labbioagentos.kernel.contracts import AnthropicEffort, GeminiThinkingLevel, ReasoningEffort
from labbioagentos.kernel.http_stream import DEFAULT_MAX_RETRIES, MAX_RETRIES_LIMIT


class _SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# Fix hints are value-free: a provider setting, URL or key never reaches output.
_PROVIDER_FIX_HINTS = {
    "PROVIDER_SETTINGS_INVALID": (
        "Check the [provider] table: api_format names one supported format, a per-format "
        "table appears only with its format, and anthropic, gemini and gemini-interactions "
        "each require theirs."
    ),
    "PROVIDER_ENDPOINT_NOT_HTTPS": (
        "Use an https endpoint. Plain http is accepted only for localhost, 127.0.0.1 and ::1; "
        "put TLS in front of the server, or reach it through an SSH tunnel on localhost."
    ),
    "PROVIDER_ENDPOINT_INVALID": (
        "Set [provider] endpoint to an http(s) URL with a host and without user information, "
        "query, fragment or whitespace."
    ),
    "PROVIDER_ENDPOINT_MOVED": (
        "Move the endpoint URL from the env file to [provider] endpoint and remove "
        "base_url_env; the env file keeps only the key."
    ),
    "PROVIDER_ENV_FILE_PERMISSIONS": (
        "Make the provider env file a regular file that you own, then run chmod 600 on it."
    ),
    "PROVIDER_ENV_FILE_UNAVAILABLE": "Create the provider env file named by [provider] env_file.",
    "PROVIDER_KEY_MISSING": (
        "Put the variable named by [provider] api_key_env in the provider env file, on one line."
    ),
    "PROVIDER_CREDENTIALS_NOT_LOADED": (
        "The provider key was not loaded before a model call; this is a composition error."
    ),
}


class ProviderConfigurationError(ValueError):
    """An unsafe or invalid provider setting, reported by a fixed code only."""

    def __init__(self, code: str, fields: tuple[str, ...] = ()):
        if code not in _PROVIDER_FIX_HINTS:
            raise ValueError("Unknown provider configuration code")
        super().__init__(code)
        self.code = code
        self.fields = fields

    @property
    def hint(self) -> str:
        return _PROVIDER_FIX_HINTS[self.code]


ApiFormat = Literal["openai", "openai-responses", "anthropic", "gemini", "gemini-interactions"]
_FORMAT_TRANSPORTS = {
    "openai": "OPENAI_COMPATIBLE_CHAT",
    "openai-responses": "OPENAI_RESPONSES",
    "anthropic": "ANTHROPIC_MESSAGES",
    "gemini": "GEMINI_GENERATE_CONTENT",
    "gemini-interactions": "GEMINI_INTERACTIONS",
}
# Settings attribute of each per-format table, keyed by its api_format value.
_FORMAT_TABLES = {
    "openai": "openai",
    "openai-responses": "openai_responses",
    "anthropic": "anthropic",
    "gemini": "gemini",
    "gemini-interactions": "gemini_interactions",
}
# Formats whose thinking settings have no safe default.
_TABLE_REQUIRED = frozenset({"anthropic", "gemini", "gemini-interactions"})
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _wire_name(value: str) -> str:
    return value.upper().replace("-", "_")


def _endpoint_error(code: str) -> PydanticCustomError:
    return PydanticCustomError(code, _PROVIDER_FIX_HINTS[code])


def _checked_endpoint(value: str) -> str:
    """Accept https anywhere and plain http only on loopback (DECIDED-C14-4)."""
    if (not value.startswith(("https://", "http://")) or "?" in value or "#" in value
            or any(character.isspace() for character in value)):
        raise _endpoint_error("PROVIDER_ENDPOINT_INVALID")
    try:
        parsed = urlsplit(value)
        parsed.port  # An unparseable port raises here.
    except ValueError:
        raise _endpoint_error("PROVIDER_ENDPOINT_INVALID") from None
    if not parsed.hostname or "@" in parsed.netloc:
        raise _endpoint_error("PROVIDER_ENDPOINT_INVALID")
    if parsed.scheme == "http" and parsed.hostname not in _LOOPBACK_HOSTS:
        raise _endpoint_error("PROVIDER_ENDPOINT_NOT_HTTPS")
    return value


class LocalOpenAIChatSettings(_SettingsModel):
    """`[provider.openai]`; the defaults are the vendor dialect."""

    thinking_parameter: Literal["thinking-object", "reasoning-effort", "omit"] = "thinking-object"
    reasoning_effort_enabled: ReasoningEffort | None = None
    reasoning_effort_disabled: ReasoningEffort | None = None
    output_limit_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    stream_include_usage: bool = Field(default=False, strict=True)

    def dialect(self) -> OpenAIChatDialect:
        return OpenAIChatDialect(
            thinking_parameter=_wire_name(self.thinking_parameter),
            reasoning_effort_enabled=self.reasoning_effort_enabled,
            reasoning_effort_disabled=self.reasoning_effort_disabled,
            output_limit_parameter=self.output_limit_parameter,
            stream_include_usage=self.stream_include_usage,
        )


class LocalOpenAIResponsesSettings(_SettingsModel):
    """`[provider.openai-responses]`; a reasoning model needs both efforts."""

    reasoning_effort_enabled: ReasoningEffort | None = None
    reasoning_effort_disabled: ReasoningEffort | None = None

    def dialect(self) -> OpenAIResponsesDialect:
        return OpenAIResponsesDialect(
            reasoning_effort_enabled=self.reasoning_effort_enabled,
            reasoning_effort_disabled=self.reasoning_effort_disabled,
        )


class LocalAnthropicSettings(_SettingsModel):
    """`[provider.anthropic]`; how a model stops thinking has no safe default."""

    thinking_off: Literal["disabled", "between-tools", "omit"]
    effort_enabled: AnthropicEffort | None = None
    effort_disabled: AnthropicEffort | None = None

    def dialect(self) -> AnthropicMessagesDialect:
        return AnthropicMessagesDialect(
            thinking_off=_wire_name(self.thinking_off),
            effort_enabled=self.effort_enabled,
            effort_disabled=self.effort_disabled,
        )


class LocalGeminiThinkingSettings(_SettingsModel):
    """`[provider.gemini]` or `[provider.gemini-interactions]`."""

    thinking_level_enabled: GeminiThinkingLevel
    thinking_level_disabled: GeminiThinkingLevel


class LocalProviderSettings(_SettingsModel):
    api_format: ApiFormat = "openai"
    endpoint: str = Field(min_length=1, max_length=2_048)
    env_file: Path
    api_key_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    model_identifier: str = Field(min_length=1, max_length=256)
    trust_env: bool = Field(default=True, strict=True)
    thinking_enabled: bool = False
    interpretation_thinking_enabled: bool = True
    provider_tool_schema_strict: bool = False
    max_output_tokens: int = Field(default=16_384, ge=256, le=32_768)
    max_model_phase_seconds: int = Field(default=300, ge=30, le=1_800)
    finalization_json_object: bool = False
    max_retries: int = Field(default=DEFAULT_MAX_RETRIES, ge=0, le=MAX_RETRIES_LIMIT, strict=True)
    openai: LocalOpenAIChatSettings | None = None
    openai_responses: LocalOpenAIResponsesSettings | None = Field(
        default=None, alias="openai-responses",
    )
    anthropic: LocalAnthropicSettings | None = None
    gemini: LocalGeminiThinkingSettings | None = None
    gemini_interactions: LocalGeminiThinkingSettings | None = Field(
        default=None, alias="gemini-interactions",
    )

    @field_validator("endpoint")
    @classmethod
    def endpoint_is_safe(cls, value: str) -> str:
        return _checked_endpoint(value)

    @model_validator(mode="after")
    def format_table_matches(self) -> "LocalProviderSettings":
        for name, attribute in _FORMAT_TABLES.items():
            if getattr(self, attribute) is not None and name != self.api_format:
                raise ValueError("A per-format table must match api_format")
        if self.api_format in _TABLE_REQUIRED and self._format_table() is None:
            raise ValueError("This api_format requires its per-format table")
        # Build the kernel dialect now so its own rules fail at load.
        self.dialect()
        return self

    def _format_table(self):
        return getattr(self, _FORMAT_TABLES[self.api_format])

    @property
    def transport(self) -> str:
        return _FORMAT_TRANSPORTS[self.api_format]

    def dialect(self):
        """The kernel dialect, or None where the transport default applies."""
        table = self._format_table()
        if self.api_format == "gemini":
            return GeminiGenerateContentDialect(**table.model_dump())
        if self.api_format == "gemini-interactions":
            return GeminiInteractionsDialect(**table.model_dump())
        return None if table is None else table.dialect()


class LocalApptainerSettings(_SettingsModel):
    binary: str = Field(default="apptainer", min_length=1, max_length=512, pattern=r"^[^-\s][^\s]*$")
    sif_store: Path
    scratch_root: Path
    cache_dir: Path | None = None
    tmp_dir: Path | None = None
    # No default: a site must state who enforces CPU, memory and process limits.
    resource_enforcement: Literal["scheduler", "apptainer-cgroups"]
    verify_sif_on_execute: bool = Field(default=True, strict=True)


class LocalExecutionSettings(_SettingsModel):
    backend: Literal["docker", "apptainer"] = "docker"
    apptainer: LocalApptainerSettings | None = None
    image_key: str
    image_reference: str
    available_python_modules: tuple[str, ...] = ()
    installed_packages: dict[str, str] = Field(default_factory=dict)
    resources: RequestedResources
    max_output_file_bytes: int = Field(default=16_777_216, ge=1, strict=True)
    max_collected_output_bytes: int = Field(default=67_108_864, ge=1, strict=True)
    tmpfs_size_mb: int = Field(default=64, ge=1, strict=True)
    include_error_context: bool = Field(default=False, strict=True)
    allow_generated_file_read: bool = Field(default=False, strict=True)

    @model_serializer(mode="wrap")
    def omit_absent_apptainer(self, handler):
        # TOML has no null; an absent optional section is omitted, not dumped as None.
        data = handler(self)
        if isinstance(data, dict) and data.get("apptainer") is None:
            data.pop("apptainer", None)
        return data

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
        if self.backend == "apptainer":
            if self.apptainer is None:
                raise ValueError("backend = 'apptainer' requires an [execution.apptainer] section")
            if re.fullmatch(r"sha256:[0-9a-f]{64}", self.image_reference) is None:
                raise ValueError("Apptainer image_reference must be a registered sha256:<64 hex> SIF")
        if self.max_output_file_bytes > self.max_collected_output_bytes:
            raise ValueError("Total collection limit must be at least the per-file limit")
        return self


def _plain_endpoint(url: str) -> bool:
    parsed = urlsplit(url)
    return (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
            and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
            and not any(char.isspace() for char in url))


class LocalClusterBuildSettings(_SettingsModel):
    """Cluster environment builds (DECIDED-C13-8); required for [environment] with Apptainer.

    Wheels are downloaded inside the base SIF on the login node or in a job on
    a network-capable partition; the build itself runs in a network-disabled job.
    """

    # CPU architecture of the compute nodes and base SIFs, as in wheel tags; no default.
    machine: str = Field(pattern=r"^[a-z0-9_]{1,32}$")
    # Node-local directory on compute nodes for the sandbox; taken literally, never
    # resolved on the login node.
    build_dir: Path = Path("/tmp")
    cpus: float = Field(default=4.0, gt=0, le=1024)
    memory_mb: int = Field(default=16_384, ge=256, le=4_194_304, strict=True)
    build_timeout_seconds: float = Field(default=7200.0, gt=0, le=86_400)
    # Largest file a build or download job may write, such as the packed SIF.
    max_file_bytes: int = Field(default=34_359_738_368, ge=1, strict=True)
    partition: str | None = None  # Build jobs; the launcher's partition when unset.
    extra_directives: tuple[str, ...] = ()
    download_placement: Literal["login", "job"] = "login"
    download_partition: str | None = None  # Required for "job": a partition with network.
    download_work_root: Path = Path("/tmp")  # Taken literally, like build_dir.
    download_timeout_seconds: float = Field(default=1800.0, ge=1, le=86_400)
    download_cpu_seconds: int = Field(default=1800, ge=1, le=86_400, strict=True)
    download_memory_mb: int = Field(default=4096, ge=256, le=1_048_576, strict=True)
    cache_dir: Path | None = None  # pip cache on the shared filesystem.
    index_url: str | None = None  # A package index without credentials.
    ca_bundle: Path | None = None  # For proxies that re-sign HTTPS.

    @model_validator(mode="after")
    def validate_build(self) -> "LocalClusterBuildSettings":
        if not self.build_dir.is_absolute() or not self.download_work_root.is_absolute():
            raise ValueError("build_dir and download_work_root must be absolute paths")
        if (self.download_placement == "job") != (self.download_partition is not None):
            raise ValueError("download_partition is required for, and only for, download_placement = 'job'")
        if self.index_url is not None and not _plain_endpoint(self.index_url):
            raise ValueError("index_url must be an HTTP(S) endpoint without credentials")
        return self


class LocalEnvironmentSettings(_SettingsModel):
    root: Path
    build_proxy: str | None = None
    build_timeout_seconds: float = Field(default=600.0, gt=0, le=3600)
    cluster: LocalClusterBuildSettings | None = None

    @model_validator(mode="after")
    def validate_proxy(self) -> "LocalEnvironmentSettings":
        if self.build_proxy is not None:
            parsed = urlsplit(self.build_proxy)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or any(char.isspace() for char in self.build_proxy)):
                raise ValueError("Build proxy must be an HTTP(S) endpoint without credentials")
        return self


class LocalLauncherSettings(_SettingsModel):
    """Where executions run: the control-plane host, or a scheduler via a template."""

    kind: Literal["local", "template"] = "local"
    # A bundled template name (slurm, pbs, lsf) or a path to a site template TOML file.
    template: str | None = Field(default=None, min_length=1, max_length=1024)
    partition: str | None = None
    account: str | None = None
    qos: str | None = None
    walltime_margin_seconds: int = Field(default=600, ge=0, le=86_400, strict=True)
    poll_interval_seconds: float = Field(default=30.0, gt=0, le=3_600)
    max_queue_seconds: float = Field(default=86_400.0, gt=0, le=30 * 86_400)
    completion_grace_seconds: float = Field(default=60.0, ge=0, le=3_600)
    extra_directives: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_kind(self) -> "LocalLauncherSettings":
        scheduler_fields = (self.template, self.partition, self.account, self.qos)
        if self.kind == "template" and self.template is None:
            raise ValueError("kind = 'template' requires a scheduler template")
        if self.kind == "local" and (any(scheduler_fields) or self.extra_directives):
            raise ValueError("Scheduler settings apply only to kind = 'template'")
        return self

    def scheduler_template(self):
        from .execution.launchers import BUILTIN_TEMPLATES, SchedulerTemplate

        if self.template in BUILTIN_TEMPLATES:
            return BUILTIN_TEMPLATES[self.template]
        path = Path(self.template)
        if path.is_symlink() or not path.is_file():
            raise ValueError("Scheduler template file is unavailable")
        return SchedulerTemplate.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))


class LocalSettings(_SettingsModel):
    runtime_backend: Literal["native"] = "native"
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
    launcher: LocalLauncherSettings | None = None

    @model_validator(mode="after")
    def validate_placement(self) -> "LocalSettings":
        # Runtime and launcher are separate axes, but only two combinations exist.
        placement = (self.execution.backend, self.launcher.kind if self.launcher else "local")
        if placement not in {("docker", "local"), ("apptainer", "template")}:
            raise ValueError(
                "Only Local + Docker and Scheduler + Apptainer are supported; "
                f"got backend {placement[0]!r} with launcher {placement[1]!r}"
            )
        if self.execution.backend == "apptainer":
            if not Path(self.execution.apptainer.binary).is_absolute():
                raise ValueError("Scheduler mode requires an absolute Apptainer binary path")
        if self.environment is not None:
            # Docker builds on the control-plane host; cluster builds need their own settings.
            if (self.execution.backend == "apptainer") != (self.environment.cluster is not None):
                raise ValueError("[environment.cluster] is required with, and only with, the Apptainer backend")
        return self

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
    required_any_capabilities: tuple[str, ...] = ()
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


def _validated_settings(raw: dict) -> LocalSettings:
    try:
        return LocalSettings.model_validate(raw)
    except ValidationError as exc:
        issues = exc.errors(include_url=False, include_input=False, include_context=False)
        provider = [item for item in issues if tuple(item["loc"][:1]) == ("provider",)]
        if not provider:
            raise
        # Only a fixed code and setting names leave this function, never values.
        code = next((item["type"] for item in provider if item["type"] in _PROVIDER_FIX_HINTS),
                    "PROVIDER_SETTINGS_INVALID")
        fields = tuple(dict.fromkeys(".".join(str(part) for part in item["loc"])
                                     for item in provider))
        raise ProviderConfigurationError(code, fields[:16]) from None


def load_settings(path: Path) -> LocalSettings:
    """Read non-secret TOML once; resolve relative paths against its directory."""
    path = path.expanduser().resolve()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw.get("provider"), dict) and "base_url_env" in raw["provider"]:
        raise ProviderConfigurationError("PROVIDER_ENDPOINT_MOVED", ("provider.base_url_env",))
    settings = _validated_settings(raw)

    def absolute(value: Path) -> Path:
        value = value.expanduser()
        return (value if value.is_absolute() else path.parent / value).resolve()

    managed_root = settings.managed_root
    if managed_root is not None:
        managed_root = managed_root.expanduser()
        managed_root = (managed_root if managed_root.is_absolute() else path.parent / managed_root).absolute()
    settings = settings.model_copy(update={
        "result_root": absolute(settings.result_root),
        "input_roots": tuple(absolute(value) for value in settings.input_roots),
        "profile": absolute(settings.profile) if settings.profile else None,
        "managed_root": managed_root,
        "gold_root": absolute(settings.gold_root) if settings.gold_root else None,
        "environment": settings.environment.model_copy(update={
            "root": absolute(settings.environment.root),
            "cluster": _absolute_cluster_build(settings.environment.cluster, absolute),
        }) if settings.environment else None,
        "provider": settings.provider.model_copy(update={"env_file": absolute(settings.provider.env_file)}),
        "execution": settings.execution.model_copy(update={
            "apptainer": _absolute_apptainer(settings.execution.apptainer, absolute),
        }) if settings.execution.apptainer else settings.execution,
        "launcher": _absolute_launcher(settings.launcher, absolute),
    })
    if settings.environment is not None and settings.environment.cluster is not None:
        # Partitions and directives are checked as the build and download jobs will use them.
        environment_scheduler_policy(settings)
        environment_scheduler_policy(settings, download=True)
    if settings.provider.env_file.exists():
        # A present env file is checked at load; an absent one fails only when
        # a command needs the key, so read-only commands work without it.
        _check_env_file(settings.provider.env_file)
    return settings


def _absolute_cluster_build(cluster: LocalClusterBuildSettings | None, absolute):
    if cluster is None:
        return None
    # Shared-filesystem paths resolve like the rest; build_dir and download_work_root
    # are node-local and stay literal.
    return cluster.model_copy(update={
        "cache_dir": absolute(cluster.cache_dir) if cluster.cache_dir else None,
        "ca_bundle": absolute(cluster.ca_bundle) if cluster.ca_bundle else None,
    })


def _absolute_launcher(launcher: LocalLauncherSettings | None, absolute):
    if launcher is None or launcher.kind != "template":
        return launcher
    from .execution.launchers import BUILTIN_TEMPLATES, validate_extra_directives

    if launcher.template not in BUILTIN_TEMPLATES:
        launcher = launcher.model_copy(update={"template": str(absolute(Path(launcher.template)))})
    validate_extra_directives(launcher.scheduler_template(), launcher.extra_directives)
    return launcher


def _absolute_apptainer(apptainer: LocalApptainerSettings, absolute) -> LocalApptainerSettings:
    return apptainer.model_copy(update={
        "sif_store": absolute(apptainer.sif_store),
        "scratch_root": absolute(apptainer.scratch_root),
        "cache_dir": absolute(apptainer.cache_dir) if apptainer.cache_dir else None,
        "tmp_dir": absolute(apptainer.tmp_dir) if apptainer.tmp_dir else None,
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


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_manifest(settings: LocalSettings) -> dict:
    """Content-derived identity; provider env values are neither read nor retained."""
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
        profile = profile.model_copy(update={"stages": tuple(
            stage.model_copy(update={"capabilities": tuple(dict.fromkeys((
                *stage.capabilities, "skill_view",
            )))}) if stage.stage in {
                WorkflowStage.EXECUTE, WorkflowStage.VALIDATE,
                WorkflowStage.INTERPRET, WorkflowStage.REPORT,
            } else stage for stage in profile.stages
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
    if settings.execution.allow_generated_file_read:
        profile = profile.model_copy(update={"stages": tuple(
            stage.model_copy(update={"capabilities": tuple(dict.fromkeys((
                *stage.capabilities, "file_read",
            )))}) if "report_read" in stage.capabilities else stage
            for stage in profile.stages
        )})
    effective_profile = profile.model_dump(mode="json")
    # Set iteration order must not change the revision across process restarts.
    for field in ("allowed_fields", "required_fields"):
        effective_profile["output_contract"][field] = sorted(effective_profile["output_contract"][field])
    source_root = Path(__file__).parent
    runtime_source = {
        "labbio_sha256": _source_digest(source_root),
        "native_kernel_sha256": _source_digest(source_root / "kernel"),
        "native_provider_sha256": _file_digest(source_root / "kernel" / "openai_chat.py"),
        "native_tool_schema_sha256": _file_digest(source_root / "runtime" / "native_tools.py"),
        "team_profile_sha256": hashlib.sha256(
            json.dumps(effective_profile, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    manifest = {
        "format_version": 1,
        "runtime_backend": settings.runtime_backend,
        "runtime_contract_version": 1,
        "configuration": settings.model_dump(mode="json"),
        "profile": effective_profile,
        "profile_sha256": hashlib.sha256(profile_bytes).hexdigest(),
        "source": runtime_source,
    }
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    manifest["runtime_revision"] = "local-" + hashlib.sha256(encoded).hexdigest()
    return manifest


def _check_env_file_stat(info: os.stat_result) -> None:
    """The env file holds the key: owned by this user and closed to everyone else."""
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise ProviderConfigurationError("PROVIDER_ENV_FILE_PERMISSIONS", ("provider.env_file",))


def _check_env_file(path: Path) -> None:
    try:
        info = path.stat()
    except OSError:
        raise ProviderConfigurationError(
            "PROVIDER_ENV_FILE_UNAVAILABLE", ("provider.env_file",),
        ) from None
    _check_env_file_stat(info)


class ProviderCredentials:
    """The provider key, held in memory only and never written to os.environ."""

    def __init__(self, settings: LocalProviderSettings):
        self._settings = settings
        self._key: SecretStr | None = None

    @property
    def loaded(self) -> bool:
        return self._key is not None

    def load(self) -> None:
        """Read the key from the env file after checking its owner and mode."""
        path = self._settings.env_file
        _check_env_file(path)
        try:
            handle = path.open("r", encoding="utf-8")
        except OSError:
            raise ProviderConfigurationError(
                "PROVIDER_ENV_FILE_UNAVAILABLE", ("provider.env_file",),
            ) from None
        with handle:
            # The open file is checked again, so a path swapped after the first
            # check cannot pass.
            _check_env_file_stat(os.fstat(handle.fileno()))
            values = dotenv_values(stream=handle, interpolate=False)
        self.provide(values.get(self._settings.api_key_env))

    def provide(self, key: str | None) -> None:
        """Hold a key supplied directly, as an embedding caller or a test does."""
        if not isinstance(key, str) or not key or any(character in key for character in "\r\n"):
            raise ProviderConfigurationError("PROVIDER_KEY_MISSING", ("provider.api_key_env",))
        self._key = SecretStr(key)

    def api_key(self) -> str:
        if self._key is None:
            raise ProviderConfigurationError("PROVIDER_CREDENTIALS_NOT_LOADED")
        return self._key.get_secret_value()

    def __repr__(self) -> str:
        return f"ProviderCredentials(loaded={self.loaded})"


class LocalProviderBuilder:
    """Build the configured format's adapter from trusted settings and the held key."""

    def __init__(self, settings: LocalProviderSettings, credentials: ProviderCredentials):
        self.settings = settings
        self.credentials = credentials

    def __call__(self, model: ModelProfile):
        settings = self.settings
        dialect = settings.dialect()
        binding = ProviderBinding(
            provider=model.provider_config.provider,
            model=model.model_identifier,
            endpoint=settings.endpoint,
            transport=settings.transport,
            **({"dialect": dialect} if dialect is not None else {}),
            thinking_mode="ENABLED" if model.thinking_enabled else "DISABLED",
            provider_tool_schema_strict=model.provider_tool_schema_strict,
            finalization_json_object=model.finalization_json_object,
        )
        return create_provider(
            binding, api_key=self.credentials.api_key(),
            trust_env=settings.trust_env, max_retries=settings.max_retries,
        )


def provider_credentials(application: LabBioApplication) -> ProviderCredentials:
    """The credential holder of an application built by `build_application`."""
    builder = application.configuration.native_provider_builder
    if not isinstance(builder, LocalProviderBuilder):
        raise ProviderConfigurationError("PROVIDER_CREDENTIALS_NOT_LOADED")
    return builder.credentials


def _load_provider(credentials: ProviderCredentials) -> None:
    """The one late point that reads the provider key."""
    credentials.load()


def scheduler_policy(settings: LocalSettings):
    """Operator scheduler policy from configuration; never from model or task text."""
    from .execution.launchers import SchedulerPolicy

    apptainer, launcher = settings.execution.apptainer, settings.launcher
    return SchedulerPolicy(
        max_output_file_bytes=settings.execution.max_output_file_bytes,
        partition=launcher.partition, account=launcher.account, qos=launcher.qos,
        walltime_margin_seconds=launcher.walltime_margin_seconds,
        poll_interval_seconds=launcher.poll_interval_seconds,
        max_queue_seconds=launcher.max_queue_seconds,
        completion_grace_seconds=launcher.completion_grace_seconds,
        extra_directives=launcher.extra_directives,
        apptainer_cache_dir=apptainer.cache_dir, apptainer_tmp_dir=apptainer.tmp_dir,
    )


def environment_scheduler_policy(settings: LocalSettings, *, download: bool = False):
    """Scheduler policy of environment build jobs, or of download jobs (DECIDED-C13-8)."""
    from .execution.launchers import SchedulerPolicy, validate_extra_directives

    apptainer, launcher = settings.execution.apptainer, settings.launcher
    cluster = settings.environment.cluster
    validate_extra_directives(launcher.scheduler_template(), cluster.extra_directives)
    partition = cluster.download_partition if download else cluster.partition
    return SchedulerPolicy(
        max_output_file_bytes=cluster.max_file_bytes,
        partition=partition or launcher.partition, account=launcher.account, qos=launcher.qos,
        walltime_margin_seconds=launcher.walltime_margin_seconds,
        poll_interval_seconds=launcher.poll_interval_seconds,
        max_queue_seconds=launcher.max_queue_seconds,
        completion_grace_seconds=launcher.completion_grace_seconds,
        extra_directives=cluster.extra_directives,
        apptainer_cache_dir=apptainer.cache_dir, apptainer_tmp_dir=apptainer.tmp_dir,
    )


def _environment_wiring(settings: LocalSettings, run_root: Path, job_ledger) -> dict:
    """Environment builds: Docker on the control-plane host, or cluster builds in jobs."""
    from .execution.environments import EnvironmentService

    environment = settings.environment
    if environment.cluster is None:
        from .execution.environment_builder import DockerEnvironmentBuilder

        def docker_service(registry):
            return EnvironmentService(
                root=environment.root, image_registry=registry,
                builder=DockerEnvironmentBuilder(
                    proxy_url=environment.build_proxy, timeout_seconds=environment.build_timeout_seconds,
                ),
                owner_user_id=settings.principal.user_id,
            )

        return {"environment_service_factory": docker_service}

    from .execution.apptainer import SifStore
    from .execution.apptainer_build import DownloadRunner
    from .execution.apptainer_environment_builder import ApptainerBuildPolicy, ApptainerEnvironmentBuilder
    from .execution.launchers import SchedulerLauncher

    apptainer, cluster = settings.execution.apptainer, environment.cluster
    template = settings.launcher.scheduler_template()
    jobs_root = run_root / "environment-jobs"
    jobs_root.mkdir(mode=0o700, exist_ok=True)
    build_launcher = SchedulerLauncher(template=template, policy=environment_scheduler_policy(settings),
                                       workspace_root=jobs_root)
    placement = (
        {"download_runner": DownloadRunner(max_file_bytes=cluster.max_file_bytes,
                                           cpu_seconds=cluster.download_cpu_seconds,
                                           cache_dir=apptainer.cache_dir, tmp_dir=apptainer.tmp_dir)}
        if cluster.download_placement == "login" else
        {"download_launcher": SchedulerLauncher(
            template=template, policy=environment_scheduler_policy(settings, download=True),
            workspace_root=jobs_root)}
    )
    policy = ApptainerBuildPolicy(
        apptainer_binary=Path(apptainer.binary), machine=cluster.machine, build_dir=cluster.build_dir,
        download_work_root=cluster.download_work_root,
        resources=RequestedResources(cpus=cluster.cpus, memory_mb=cluster.memory_mb,
                                     timeout_seconds=cluster.build_timeout_seconds),
        download_timeout_seconds=cluster.download_timeout_seconds,
        download_memory_mb=cluster.download_memory_mb, cache_dir=cluster.cache_dir,
        index_url=cluster.index_url, proxy_url=environment.build_proxy, ca_bundle=cluster.ca_bundle,
    )

    def cluster_service(registry):
        return EnvironmentService(
            root=environment.root, image_registry=registry,
            builder=ApptainerEnvironmentBuilder(
                sif_store=SifStore(apptainer.sif_store), policy=policy, build_launcher=build_launcher,
                job_ledger=job_ledger, **placement,
            ),
            owner_user_id=settings.principal.user_id,
        )

    # Crash cleanup reaches build and download jobs through the launcher of their shared root.
    return {"environment_service_factory": cluster_service, "environment_job_launcher": build_launcher}


def _cluster_execution(settings: LocalSettings, run_root: Path) -> dict:
    """Scheduler + Apptainer wiring; the capability probe runs before the first execution."""
    from .execution.apptainer import ApptainerCommandBuilder, SifStore, SubprocessApptainerRunner
    from .execution.apptainer_probe import probe_capabilities
    from .execution.job_ledger import JobLedger
    from .execution.launchers import SchedulerLauncher

    apptainer, launcher = settings.execution.apptainer, settings.launcher
    image = settings.execution.approved_image()
    store = SifStore(apptainer.sif_store)
    runner = SubprocessApptainerRunner(
        max_output_file_bytes=settings.execution.max_output_file_bytes,
        cache_dir=apptainer.cache_dir, tmp_dir=apptainer.tmp_dir,
    )

    def backend_check() -> dict:
        sif_path = (store.verify(image.reference) if apptainer.verify_sif_on_execute
                    else store.path_for(image.reference))
        capabilities = probe_capabilities(
            binary=apptainer.binary, sif_path=sif_path, executable=image.executable[0],
            runner=runner, scratch_root=apptainer.scratch_root,
        )
        capabilities.require(resource_enforcement=apptainer.resource_enforcement)
        return capabilities.trace_record(resource_enforcement=apptainer.resource_enforcement)

    policy = scheduler_policy(settings)
    return {
        # The login node runs only the data-free import probe through this runner.
        "process_runner": runner,
        "command_builder": ApptainerCommandBuilder(
            store, scratch_root=apptainer.scratch_root,
            resource_enforcement=apptainer.resource_enforcement,
            apptainer_binary=apptainer.binary,
            verify_sif_on_execute=apptainer.verify_sif_on_execute,
        ),
        "backend_check": backend_check,
        "job_launcher": SchedulerLauncher(
            template=launcher.scheduler_template(), policy=policy,
            workspace_root=run_root / "executions",
        ),
        "job_ledger": JobLedger(run_root / "jobs.sqlite"),
    }


def build_application(
    settings: LocalSettings, run_root: Path, *, load_provider: bool = True,
) -> LabBioApplication:
    """Assemble known owners without selecting methods or installing dependencies."""
    manifest = runtime_manifest(settings)
    profile = _LocalProfile.model_validate_json(json.dumps(manifest["profile"]))
    credentials = ProviderCredentials(settings.provider)
    if load_provider:
        _load_provider(credentials)
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
            provider_config=ProviderConfigRef(
                config_id="local-provider", provider=settings.provider.api_format,
            ),
            # A legacy descriptor; only Chat Completions has a named value.
            transport=(ProviderTransport.OPENAI_CHAT_COMPLETIONS
                       if settings.provider.api_format == "openai" else ProviderTransport.AUTO),
            thinking_enabled=thinking,
            private_tool_reasoning_continuity=thinking,
            provider_tool_schema_strict=settings.provider.provider_tool_schema_strict,
            thinking_wire_format=ProviderThinkingWireFormat.TYPE_OBJECT,
            max_output_tokens=settings.provider.max_output_tokens,
            finalization_json_object=settings.provider.finalization_json_object,
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
            + (
                " CONTROL_STATE_REQUIRED_CAPABILITIES="
                + json.dumps(stage.required_capabilities)
                + ". Complete every listed capability successfully before ending "
                "capability mode; allowed capabilities not listed here remain optional."
                if stage.required_capabilities else ""
            )
            + (
                " CONTROL_STATE_REQUIRED_ANY_CAPABILITIES="
                + json.dumps(stage.required_any_capabilities)
                + ". Complete at least one listed capability successfully before ending "
                "capability mode; the others remain optional."
                if stage.required_any_capabilities else ""
            )
        )} if stage.capabilities else {},
        finalization_prompt_values={"protocol": (
            shared_context + profile.finalization_protocol.format(stage=stage.stage.value)
        )},
        capability_phase_enabled=bool(stage.capabilities),
        preserve_capability_completion=bool(stage.capabilities),
        required_capabilities=stage.required_capabilities,
        required_any_capabilities=stage.required_any_capabilities,
        # REPORT must both establish complete citation coverage and submit the
        # final prose. Its accumulated governed pages make the token ledger,
        # rather than tool-turn count, the tighter bound in real runs.
        max_capability_turns=24 if stage.stage is WorkflowStage.REPORT else 16,
        max_no_progress_seconds=settings.provider.max_model_phase_seconds,
        retry_enabled=stage.stage is not WorkflowStage.VALIDATE,
        user_input_enabled=stage.user_input_enabled,
        clarification_enabled=stage.clarification_enabled,
    ) for stage in profile.stages)

    def observe(kind: str, value: object) -> None:
        payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        with (run_root / "model-boundaries.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": kind, "payload": payload}, sort_keys=True) + "\n")

    resources = settings.execution.resources
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
        execution_wiring = {}
        if settings.execution.backend == "apptainer":
            execution_wiring = _cluster_execution(settings, run_root)
            cleanup.callback(execution_wiring["job_ledger"].close)
        environment_wiring = {}
        if settings.environment is not None:
            # Build jobs share the execution ledger, so the run's crash cleanup covers them.
            environment_wiring = _environment_wiring(settings, run_root, execution_wiring.get("job_ledger"))
        application = LabBioApplication(ApplicationRuntimeConfiguration(
            artifact_root=run_root / "artifacts", execution_workspace_root=run_root / "executions",
            runtime_revision=manifest["runtime_revision"], allowed_input_roots=settings.input_roots,
            runtime_backend=RuntimeBackend(settings.runtime_backend),
            native_provider_builder=LocalProviderBuilder(settings.provider, credentials),
            projects=(Project(project_id=settings.workspace.project_id, lab_id=settings.workspace.lab_id,
                              owner_user_id=settings.principal.user_id),),
            profile_catalog=catalog, stage_assemblies=assemblies,
            approved_images=(settings.execution.approved_image(),), output_contracts=(profile.output_contract,),
            exposure_policy=ExposurePolicy(
                allow_generated_file_read=settings.execution.allow_generated_file_read,
            ),
            execution_policy=ExecutionPolicy(
                allow_network=False, max_cpus=resources.cpus, max_memory_mb=resources.memory_mb,
                max_pids=resources.pids_limit, max_timeout_seconds=resources.timeout_seconds,
                max_output_file_bytes=settings.execution.max_output_file_bytes,
                max_collected_output_bytes=settings.execution.max_collected_output_bytes,
                tmpfs_size_mb=settings.execution.tmpfs_size_mb,
                include_error_context=settings.execution.include_error_context,
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
            literature_search=LiteratureSearchService(),
            **execution_wiring,
            **environment_wiring,
        ))
        cleanup.pop_all()  # The caller now owns both stores, including failure cleanup.
        return application
